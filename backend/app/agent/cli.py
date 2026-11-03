"""Run the investigation agent on one case from the terminal.

    python -m backend.app.agent.cli --list
    python -m backend.app.agent.cli <case id or prefix> [--mode team|single] [--max-steps 8] [--model qwen3:8b]
"""
import argparse
import sys
import time

from ..config import Settings
from ..database import CaseDatabase
from ..models import AgentStep
from ..services.jira import JiraEscalationPublisher
from ..services.memory import LocalEmbedder
from .graph import run_investigation
from .team import run_team
from .tools import InvestigationToolbox


def _print_step(step: AgentStep) -> None:
    args = ", ".join(f"{k}={v!r}" for k, v in step.arguments.items() if v not in ("", None))
    status = "ok" if step.ok else "FAILED"
    print(f"  [{step.index}] {step.agent:<8} {step.tool}({args})  {status}, {step.duration_ms} ms")
    preview = step.result.replace("\n", " ")
    print(f"      -> {preview[:220]}{'...' if len(preview) > 220 else ''}")


def main() -> int:
    parser = argparse.ArgumentParser(description="RootIQ investigation agent")
    parser.add_argument("case_id", nargs="?", help="case id or unique prefix")
    parser.add_argument("--list", action="store_true", help="list open cases and exit")
    parser.add_argument("--mode", choices=["team", "single"], default="team", help="multi-agent team or single agent")
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--model", default=None)
    args = parser.parse_args()

    settings = Settings()
    database = CaseDatabase(settings.database_target)
    database.initialize()
    cases = database.list_cases()

    if args.list or not args.case_id:
        for case in cases:
            if case.status != "resolved":
                print(f"{case.id[:8]}  {case.service:<16} {case.status:<18} {case.title}")
        return 0

    matches = [c for c in cases if c.id.startswith(args.case_id)]
    if len(matches) != 1:
        print(f"No single case matches '{args.case_id}'.", file=sys.stderr)
        return 1
    case = matches[0]
    model = args.model or settings.agent_model
    max_steps = args.max_steps or settings.agent_max_steps

    jira = JiraEscalationPublisher(
        settings.jira_site_url, settings.jira_email, settings.jira_api_token,
        settings.jira_project_key, settings.jira_issue_type,
    )
    box = InvestigationToolbox(
        case, database, LocalEmbedder(settings.ollama_url, settings.embedding_model), jira,
        settings.logs_dir, on_step=_print_step,
    )
    print(f"Investigating {case.id[:8]}: {case.title}  ({args.mode} mode, model {model})\n")
    started = time.perf_counter()
    if args.mode == "single":
        result = run_investigation(box, model, max_steps, base_url=settings.ollama_url)
        print(f"\nFinished in {time.perf_counter() - started:.1f}s, {result.tool_rounds} tool rounds"
              f"{' (stopped at the limit)' if result.stopped_by_limit else ''}.\n")
    else:
        result = run_team(box, model, base_url=settings.ollama_url,
                          time_limit_seconds=settings.agent_time_limit_seconds or None)
        print(f"\nFinished in {time.perf_counter() - started:.1f}s.\n")
        print("Lead's hypotheses:")
        for h in result.plan.hypotheses:
            print(f"  - {h}")
        for i, verdict in enumerate(result.reviews, 1):
            outcome = "approved" if verdict.approved else f"rejected, recheck {', '.join(verdict.recheck)}"
            print(f"Review {i}: {outcome}")
            for claim in verdict.unsupported_claims:
                print(f"    unsupported: {claim}")
            if verdict.instructions and not verdict.approved:
                print(f"    instructions: {verdict.instructions}")
        if result.severity_note:
            print(f"Severity guard: {result.severity_note}")
        for warning in result.warnings:
            print(f"Warning: {warning}")
        print()

    a = result.analysis
    print(f"Summary:  {a.incident_summary}\nCause:    {a.likely_cause}\nSeverity: {a.suggested_severity}")
    print(f"Workaround: {a.workaround}")
    for i, step in enumerate(a.diagnostic_steps, 1):
        print(f"  check {i}. {step}")
    print(f"\nProposed actions awaiting approval ({len(result.proposals)}), none executed:")
    for p in result.proposals:
        print(f"  - [{p.kind}] {p.summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
