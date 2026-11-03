"""Carries out the proposals that an engineer approved. Nothing here runs without the decision of a person.

In dry-run mode, the default, the app records each approved action and what it would do. It calls no external
system. Live mode makes a Gmail draft, which it does not send, or adds a Jira comment.
"""
from ..models import ActionDecision, ActionOutcome, ProposedAction
from .plaintext import plain_text

EDITABLE_FIELDS = {"customer_update": {"subject", "body"}, "jira_comment": {"comment"}, "remediation": {"params"}, "code_change": set()}
TEXT_FIELDS = {"customer_update": {"subject", "body"}, "jira_comment": {"comment"}, "remediation": set(), "code_change": set()}


class ActionExecutor:
    def __init__(self, gmail, jira, dry_run: bool, actor: str, remediate=None) -> None:
        """remediate(index, proposal, payload) -> carries out a remediation. See followup.py. Status, detail."""
        self.gmail = gmail
        self.jira = jira
        self.dry_run = dry_run
        self.actor = actor
        self.remediate = remediate

    def __call__(self, proposals: list[ProposedAction], decisions: list[ActionDecision]) -> list[ActionOutcome]:
        by_index = {d.index: d for d in decisions}
        return [self._one(i, proposal, by_index.get(i)) for i, proposal in enumerate(proposals)]

    def _one(self, index: int, proposal: ProposedAction, decision: ActionDecision | None) -> ActionOutcome:
        if decision is None or decision.action == "reject":
            return ActionOutcome(
                index=index, kind=proposal.kind, decision=decision.action if decision else "none",
                status="skipped", detail="Rejected by engineer." if decision else "No decision recorded.",
            )
        payload = dict(proposal.payload)
        if decision.action == "edit":
            allowed = EDITABLE_FIELDS[proposal.kind]
            payload.update({k: v for k, v in decision.payload.items() if k in allowed})
        # Older runs can have Markdown in their text. Remove it before the text goes out.
        payload = {k: plain_text(v) if k in TEXT_FIELDS[proposal.kind] and isinstance(v, str) else v for k, v in payload.items()}
        try:
            if proposal.kind == "remediation":
                if self.remediate is None:
                    status, detail = "skipped", "Remediation is off."
                else:
                    status, detail = self.remediate(index, proposal, payload)
            else:
                status, detail = self._perform(proposal.kind, payload)
        except Exception as exc:  # report an integration failure; do not try again silently
            status, detail = "failed", str(exc)
        return ActionOutcome(index=index, kind=proposal.kind, decision=decision.action, status=status, detail=detail)

    def _perform(self, kind: str, payload: dict[str, str]) -> tuple[str, str]:
        if kind == "customer_update":
            if self.dry_run:
                return "dry_run", f"Would create a Gmail draft: \"{payload['subject']}\"."
            draft_id = self.gmail.create_draft(payload["subject"], payload["body"])
            return "executed", f"Gmail draft created ({draft_id}); it has not been sent."
        if kind == "jira_comment":
            key = payload.get("issue_key", "")
            if not key:
                return "failed", "The case has no Jira ticket to comment on."
            if self.dry_run:
                return "dry_run", f"Would comment on {key}."
            self.jira.add_comment(key, self.actor, payload["comment"])
            return "executed", f"Comment posted on {key}."
        if kind == "code_change":  # the app never writes code: the patch waits for a person to apply it
            return "executed", f"Patch for {payload.get('path', 'the file')} approved. It is not applied: download it and apply it in the repository."
        return "failed", f"Unknown action kind '{kind}'."
