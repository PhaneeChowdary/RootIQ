"""Check the Jira settings in backend/.env without a change in Jira.

    .venv/bin/python -m backend.app.jira_check

Each call is a read: a GET, or the read-only search endpoint. Each failed step prints what to fix.
"""

import base64
import json
import sys
from urllib import error, parse, request

from .config import Settings
from .services.jira import JiraEscalationPublisher

settings = Settings()
OK, FAIL, WARN = "  ok  ", " FAIL ", " warn "


def _call(path: str, body: dict | None = None) -> tuple[int | None, object]:
    creds = base64.b64encode(f"{settings.jira_email}:{settings.jira_api_token}".encode()).decode()
    req = request.Request(
        settings.jira_site_url.rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Basic {creds}", "Accept": "application/json", "Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    try:
        with request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except error.HTTPError as exc:
        text = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(text)
        except ValueError:
            return exc.code, text[:200]
    except (error.URLError, TimeoutError) as exc:
        return None, str(exc)


def main() -> int:
    missing = [name for name, value in (
        ("ROOTIQ_JIRA_SITE_URL", settings.jira_site_url), ("ROOTIQ_JIRA_EMAIL", settings.jira_email),
        ("ROOTIQ_JIRA_API_TOKEN", settings.jira_api_token), ("ROOTIQ_JIRA_PROJECT_KEY", settings.jira_project_key),
    ) if not value]
    if missing:
        print(f"{FAIL} Missing in backend/.env: {', '.join(missing)}")
        return 1
    print(f"Site {settings.jira_site_url}  project {settings.jira_project_key}  issue type {settings.jira_issue_type}\n")

    status, body = _call("/rest/api/3/serverInfo")
    if status != 200:
        detail = body.get("errorMessage") if isinstance(body, dict) else body
        print(f"{FAIL} Site does not answer ({status}: {detail}).")
        print("       'Site temporarily unavailable' means Atlassian deactivated or deleted the site (inactive free")
        print("       sites get deactivated). Check admin.atlassian.com: restore it there, or create a new free site")
        print("       and put its URL in ROOTIQ_JIRA_SITE_URL.")
        return 1
    print(f"{OK} Site is up ({body.get('deploymentType', 'Cloud')}).")

    status, body = _call("/rest/api/3/myself")
    if status != 200:
        print(f"{FAIL} Sign-in rejected ({status}). The email must be your Atlassian account email, and the token")
        print("       must come from id.atlassian.com/manage-profile/security/api-tokens (tokens expire; make a new one).")
        return 1
    print(f"{OK} Signed in as {body.get('displayName')}.")

    key = settings.jira_project_key
    status, body = _call(f"/rest/api/3/project/{parse.quote(key)}")
    if status != 200:
        _, projects = _call("/rest/api/3/project/search")
        keys = [p["key"] for p in projects.get("values", [])] if isinstance(projects, dict) else []
        print(f"{FAIL} Project {key} not found ({status}). Projects you can see: {', '.join(keys) or 'none'}.")
        print("       Create a project with this key, or set ROOTIQ_JIRA_PROJECT_KEY to one of the keys above.")
        return 1
    style = body.get("style", "classic")
    print(f"{OK} Project {key}: {body.get('name')} ({'team-managed' if style == 'next-gen' else 'company-managed'}).")

    problems = 0
    status, body = _call(f"/rest/api/3/issue/createmeta/{parse.quote(key)}/issuetypes")
    types = {t["name"]: t["id"] for t in body.get("issueTypes", body.get("values", []))} if isinstance(body, dict) else {}
    if settings.jira_issue_type not in types:
        print(f"{FAIL} Issue type '{settings.jira_issue_type}' is not in {key}. Available: {', '.join(types) or 'none'}.")
        print("       Set ROOTIQ_JIRA_ISSUE_TYPE to one of these.")
        return 1
    print(f"{OK} Issue type '{settings.jira_issue_type}' can be created.")

    status, body = _call(f"/rest/api/3/issue/createmeta/{parse.quote(key)}/issuetypes/{types[settings.jira_issue_type]}")
    fields = {f.get("fieldId"): f for f in body.get("fields", body.get("values", []))} if isinstance(body, dict) else {}
    priority = fields.get("priority")
    if not priority:
        problems += 1
        print(f"{FAIL} The Priority field is not on the '{settings.jira_issue_type}' create screen, so RootIQ cannot create")
        print("       tickets (it sets a priority from the case severity). In a team-managed project: Project settings >")
        print(f"       Issue types > {settings.jira_issue_type} > add Priority. Company-managed projects have it by default.")
    else:
        names = {v.get("name") for v in priority.get("allowedValues", [])}
        needed = set(JiraEscalationPublisher._PRIORITY_MAP.values())
        if names and not needed <= names:
            problems += 1
            print(f"{FAIL} Priorities {', '.join(sorted(needed - names))} are missing. Jira has: {', '.join(sorted(names))}.")
        else:
            print(f"{OK} Priority field is available ({', '.join(sorted(needed))}).")

    status, body = _call("/rest/api/3/search/jql", {"jql": f"project = {key} ORDER BY created DESC", "maxResults": 1, "fields": ["status"]})
    if status != 200:
        problems += 1
        print(f"{FAIL} Ticket search failed ({status}). Check that your account can browse project {key}.")
    else:
        issues = body.get("issues", [])
        print(f"{OK} Ticket search works ({'latest ' + issues[0]['key'] if issues else 'no tickets yet'}).")
        if issues:
            _, trans = _call(f"/rest/api/3/issue/{issues[0]['key']}/transitions")
            names = [t.get("name", "") for t in trans.get("transitions", [])] if isinstance(trans, dict) else []
            if any("done" in n.lower() for n in names):
                print(f"{OK} A 'Done' transition exists, so resolving a case can close its ticket.")
            else:
                print(f"{WARN} No transition named 'Done' from {issues[0]['key']} ({', '.join(names) or 'none'}).")
                print("       Resolving a case will leave the ticket open. Add a Done status to the workflow to fix this.")

    print("\nReady. Restart the backend to pick up any .env changes." if not problems else f"\n{problems} problem(s) to fix.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
