import base64
import json
import re
from urllib import error, request

from ..models import SupportCase

NO_CUSTOMER = "No customer named"  # a ticket that names no customer: the team writes no customer email


class JiraPublishError(RuntimeError):
    pass


class JiraEscalationPublisher:
    def __init__(self, site_url: str, email: str, api_token: str, project_key: str, issue_type: str):
        self.site_url = site_url.rstrip("/")
        self.email = email
        self.api_token = api_token
        self.project_key = project_key
        self.issue_type = issue_type

    @property
    def configured(self) -> bool:
        return all((self.site_url, self.email, self.api_token, self.project_key))

    def issue_url(self, key: str) -> str:
        return f"{self.site_url}/browse/{key}"

    def search_issues(self, jql: str, max_results: int = 50) -> list[dict[str, str]]:
        if not self.configured:
            raise JiraPublishError("Jira not configured.")
        full_jql = f"project = {self.project_key} AND {jql}"
        body = json.dumps({
            "jql": full_jql,
            "maxResults": max_results,
            "fields": ["summary", "description", "status", "priority", "reporter", "created", "labels", "components"],
        })
        creds = base64.b64encode(f"{self.email}:{self.api_token}".encode()).decode()
        req = request.Request(
            f"{self.site_url}/rest/api/3/search/jql",
            data=body.encode(),
            headers={"Authorization": f"Basic {creds}", "Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except error.HTTPError as exc:
            raise JiraPublishError(f"Jira search failed ({exc.code}).") from exc
        except (error.URLError, TimeoutError) as exc:
            raise JiraPublishError("Cannot reach Jira.") from exc
        tickets: list[dict[str, str]] = []
        for issue in data.get("issues", []):
            fields = issue.get("fields", {})
            desc = fields.get("description")
            tickets.append({
                "key": issue["key"],
                "summary": fields.get("summary", ""),
                "description": self._extract_text(desc) if isinstance(desc, dict) else (desc or ""),
                "status": (fields.get("status") or {}).get("name", ""),
                "priority": (fields.get("priority") or {}).get("name", ""),
                "reporter": (fields.get("reporter") or {}).get("displayName", ""),
                "created": fields.get("created", ""),
                "labels": ",".join(fields.get("labels") or []),
                "components": ",".join(c.get("name", "") for c in fields.get("components") or []),
            })
        return tickets

    @staticmethod
    def case_fields(ticket: dict[str, str], known_services: set[str]) -> tuple[str, str]:
        """Find the service and the customer of an imported ticket.

        Service: a Jira component or label that is a known service, then a "Service:" line, then a known service
        name in the text. If none, "unknown". Customer: a "Customer:" line, then the "RootIQ case for:" line of a
        ticket that RootIQ made, else NO_CUSTOMER. Not the reporter: that is the person who wrote the ticket, often
        someone of the support team, not the customer.
        """
        known = {s.lower() for s in known_services}
        text = f"{ticket.get('summary', '')}\n{ticket.get('description', '')}"
        tags = [t.strip() for t in f"{ticket.get('components', '')},{ticket.get('labels', '')}".split(",") if t.strip()]
        service = next((t.lower() for t in tags if t.lower() in known), "")
        if not service:
            line = re.search(r"^\s*service\s*:\s*([\w.-]+)", text, re.I | re.M)
            service = line.group(1).lower() if line else ""
        if not service:
            service = next((s for s in sorted(known, key=len, reverse=True) if re.search(rf"\b{re.escape(s)}\b", text, re.I)), "unknown")
        line = (re.search(r"^\s*customer\s*:\s*(.+?)\s*$", text, re.I | re.M)
                or re.search(r"^\s*RootIQ (?:case|incident) for\s*:\s*(.+?)\s*$", text, re.I | re.M))
        customer = line.group(1).strip() if line else NO_CUSTOMER
        return service, customer

    @staticmethod
    def _extract_text(doc: dict) -> str:
        parts: list[str] = []
        for block in doc.get("content", []):
            for inline in block.get("content", []):
                if inline.get("type") == "text":
                    parts.append(inline.get("text", ""))
        return "\n".join(parts)

    _PRIORITY_MAP = {
        "critical": "Highest",
        "high": "High",
        "medium": "Medium",
        "low": "Low",
    }

    _SEVERITY_MAP = {
        "highest": "critical",
        "high": "high",
        "medium": "medium",
        "low": "low",
        "lowest": "low",
    }

    def map_severity(self, jira_priority: str) -> str:
        return self._SEVERITY_MAP.get(jira_priority.lower(), "medium")

    def create_tracking_issue(self, title: str, customer: str, service: str, environment: str, reported_issue: str, severity: str) -> tuple[str, str]:
        if not self.configured:
            raise JiraPublishError("Jira not configured.")
        description_lines = [
            f"RootIQ case for: {customer}",
            f"Service: {service}  |  Environment: {environment}",
            reported_issue,
        ]
        fields: dict[str, object] = {
            "project": {"key": self.project_key},
            "issuetype": {"name": self.issue_type},
            "summary": title,
            "description": self._document(description_lines),
            "priority": {"name": self._PRIORITY_MAP.get(severity, "Medium")},
        }
        return self._submit_issue(fields)

    def create_issue(self, case: SupportCase) -> tuple[str, str]:
        if not self.configured:
            raise JiraPublishError("Jira not configured.")
        description_lines = [
            f"RootIQ incident for: {case.customer}",
            f"Service: {case.service}  |  Environment: {case.environment}  |  Severity: {case.severity}",
            f"Impact: {case.analysis.customer_impact}",
            f"Likely cause: {case.analysis.likely_cause}",
            f"Workaround: {case.analysis.workaround}",
        ]
        fields: dict[str, object] = {
            "project": {"key": self.project_key},
            "issuetype": {"name": self.issue_type},
            "summary": case.title,
            "description": self._document(description_lines),
            "priority": {"name": self._PRIORITY_MAP.get(case.severity, "Medium")},
        }
        return self._submit_issue(fields)

    def set_priority(self, issue_key: str, severity: str) -> None:
        """Change the ticket priority to match the case severity."""
        creds = base64.b64encode(f"{self.email}:{self.api_token}".encode()).decode()
        req = request.Request(
            f"{self.site_url}/rest/api/3/issue/{issue_key}",
            data=json.dumps({"fields": {"priority": {"name": self._PRIORITY_MAP.get(severity, "Medium")}}}).encode(),
            headers={"Authorization": f"Basic {creds}", "Content-Type": "application/json", "Accept": "application/json"},
            method="PUT",
        )
        try:
            with request.urlopen(req, timeout=30):
                pass
        except (error.HTTPError, error.URLError, TimeoutError) as exc:
            raise JiraPublishError(f"Cannot change the priority of {issue_key}.") from exc

    def transition_to_done(self, issue_key: str) -> None:
        self._transition(issue_key, "done")

    def transition_to_open(self, issue_key: str) -> None:
        """Back to a to-do status, for a case that comes back from the recycle bin."""
        self._transition(issue_key, "new")

    def _transition(self, issue_key: str, category: str) -> None:
        """Move the ticket to a status of this Jira status category: "new", "indeterminate" or "done". To do.
        The names of the statuses differ between workflows; the categories do not. Nothing happens when the ticket
        is in that category already or the workflow has no such transition."""
        if not self.configured:
            raise JiraPublishError("Jira not configured.")
        creds = base64.b64encode(f"{self.email}:{self.api_token}".encode()).decode()
        headers = {"Authorization": f"Basic {creds}", "Content-Type": "application/json", "Accept": "application/json"}
        trans_req = request.Request(
            f"{self.site_url}/rest/api/3/issue/{issue_key}/transitions",
            headers=headers, method="GET",
        )
        try:
            with request.urlopen(trans_req, timeout=30) as resp:
                transitions = json.loads(resp.read()).get("transitions", [])
        except (error.HTTPError, error.URLError, TimeoutError) as exc:
            raise JiraPublishError(f"Cannot fetch transitions for {issue_key}.") from exc
        in_category = [t for t in transitions if ((t.get("to") or {}).get("statusCategory") or {}).get("key") == category]
        named = [t for t in transitions if category == "done" and "done" in t.get("name", "").lower()]
        done_id = next((t["id"] for t in in_category or named), None)
        if done_id is None:
            return
        do_req = request.Request(
            f"{self.site_url}/rest/api/3/issue/{issue_key}/transitions",
            data=json.dumps({"transition": {"id": done_id}}).encode(),
            headers=headers, method="POST",
        )
        try:
            with request.urlopen(do_req, timeout=30):
                pass
        except (error.HTTPError, error.URLError, TimeoutError) as exc:
            raise JiraPublishError(f"Cannot move {issue_key} to {'Done' if category == 'done' else 'To Do'}.") from exc

    def get_comments(self, issue_key: str) -> list[dict[str, str]]:
        if not self.configured:
            raise JiraPublishError("Jira not configured.")
        creds = base64.b64encode(f"{self.email}:{self.api_token}".encode()).decode()
        req = request.Request(
            f"{self.site_url}/rest/api/3/issue/{issue_key}/comment",
            headers={"Authorization": f"Basic {creds}", "Accept": "application/json"},
            method="GET",
        )
        try:
            with request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except (error.HTTPError, error.URLError, TimeoutError) as exc:
            raise JiraPublishError(f"Cannot fetch comments for {issue_key}.") from exc
        comments: list[dict[str, str]] = []
        for c in data.get("comments", []):
            body = c.get("body")
            author_info = c.get("author") or {}
            comments.append({
                "id": c.get("id", ""),
                "author": author_info.get("displayName", ""),
                "author_email": author_info.get("emailAddress", ""),
                "body": self._extract_text(body) if isinstance(body, dict) else (body or ""),
                "created": c.get("created", ""),
            })
        return comments

    def add_comment(self, issue_key: str, author: str, message: str) -> None:
        if not self.configured:
            raise JiraPublishError("Jira not configured.")
        body = json.dumps({"body": self._document([f"[{author}] {message}"])})
        creds = base64.b64encode(f"{self.email}:{self.api_token}".encode()).decode()
        req = request.Request(
            f"{self.site_url}/rest/api/3/issue/{issue_key}/comment",
            data=body.encode(),
            headers={"Authorization": f"Basic {creds}", "Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=30):
                pass
        except (error.HTTPError, error.URLError, TimeoutError) as exc:
            raise JiraPublishError(f"Cannot add comment to {issue_key}.") from exc

    def _submit_issue(self, fields: dict[str, object]) -> tuple[str, str]:
        creds = base64.b64encode(f"{self.email}:{self.api_token}".encode()).decode()
        req = request.Request(
            f"{self.site_url}/rest/api/3/issue",
            data=json.dumps({"fields": fields}).encode(),
            headers={"Authorization": f"Basic {creds}", "Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=30) as resp:
                issue = json.loads(resp.read())
            key = issue["key"]
            return key, f"{self.site_url}/browse/{key}"
        except error.HTTPError as exc:
            raise JiraPublishError(f"Jira rejected the request ({exc.code}).") from exc
        except (error.URLError, TimeoutError) as exc:
            raise JiraPublishError("Cannot reach Jira.") from exc
        except KeyError as exc:
            raise JiraPublishError("Unexpected Jira response.") from exc

    @staticmethod
    def _document(lines: list[str]) -> dict[str, object]:
        return {
            "type": "doc",
            "version": 1,
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": line}]}
                for line in lines
            ],
        }
