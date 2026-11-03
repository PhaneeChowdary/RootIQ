"""Compare the one-prompt analysis, the single agent and the agent team on the evaluation scenarios.

    python -m backend.evals.run_evals                                   # each scenario, all three modes
    python -m backend.evals.run_evals --modes team --only rec-otp-429   # a subset
    python -m backend.evals.run_evals --resume reports/evals-<stamp>.json

Continuous evaluation, in CI: .github/workflows/ci.yml
    python -m backend.evals.run_evals --modes team --cloud --offline-memory --only <ids> --gate
--cloud uses the model chains of the app: Groq, Gemini, then local. --offline-memory needs no Ollama. --gate compares
the score of the agent team with backend/evals/baseline.json: a drop of more than 0.10 fails the run with exit code 2.
--save-baseline writes the score of this run as the new baseline.

Each scenario runs in its own temporary database and log folder, so the results never touch real data.
The app saves the results as JSON after each run, and writes a Markdown summary next to them.
"""
import argparse
import json
import re
import shutil
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from ..app.agent.graph import run_investigation
from ..app.agent.team import run_team
from ..app.agent.telemetry import UsageRecorder
from ..app.agent.tools import InvestigationToolbox
from ..app.config import BACKEND_ROOT, Settings
from ..app.database import CaseDatabase
from ..app.models import CaseCreate
from ..app.services.analysis import OllamaSupportAnalyst
from ..app.services.memory import LocalEmbedder
from ..seed.seed_demo import seed
from .scenarios import SCENARIOS, UNKNOWN_TERMS, Scenario

MODES = ("single_shot", "agent", "team")
MODE_NAMES = {"single_shot": "One prompt", "agent": "Single agent", "team": "Agent team"}
REPORTS = BACKEND_ROOT.parent / "reports"
TIMESTAMP = re.compile(r"\d{2}:\d{2}(:\d{2})?")


class NoJira:
    configured = False


def score(scenario: Scenario, cause: str) -> dict:
    text = cause.lower()
    if scenario.kind == "no_evidence":
        honest = any(term in text for term in UNKNOWN_TERMS)
        verdict = "correct" if honest else "wrong"
    else:
        hits = sum(any(term.lower() in text for term in group) for group in scenario.cause)
        verdict = "correct" if hits == len(scenario.cause) else "partial" if hits else "wrong"
    copied = verdict != "correct" and any(term.lower() in text for term in scenario.lookalike)
    return {"verdict": verdict, "copied_lookalike": copied, "cites_evidence": bool(TIMESTAMP.search(cause))}


BASELINE = Path(__file__).with_name("baseline.json")
TOLERANCE = 0.10
POINTS = {"correct": 1.0, "partial": 0.5, "wrong": 0.0, "error": 0.0}


class OfflineEmbedder:
    """Word-count vectors: the knowledge-base search without Ollama. Worse than real embeddings, but stable. CI."""

    def __init__(self, size: int = 256) -> None:
        self.size = size

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.size
        for word in re.findall(r"[a-z0-9]{3,}", text.lower()):
            vector[sum(map(ord, word)) % self.size] += 1.0
        return vector


def team_score(results: list[dict]) -> float | None:
    team = [r for r in results if r["mode"] == "team"]
    return round(sum(POINTS[r["verdict"]] for r in team) / len(team), 3) if team else None


def gate(score: float | None, baseline: dict) -> tuple[bool, str]:
    """Pass when the score is not more than TOLERANCE below the baseline."""
    if score is None:
        return False, "No agent team results to compare."
    floor = round(float(baseline.get("team_score", 0)) - TOLERANCE, 3)
    if score < floor:
        return False, f"Agent team score {score} is below the baseline {baseline.get('team_score')} - {TOLERANCE} = {floor}."
    return True, f"Agent team score {score} (baseline {baseline.get('team_score')}, floor {floor})."


def run_one(scenario: Scenario, mode: str, template: Path, settings: Settings, embedder, llm=None) -> dict:
    work = Path(tempfile.mkdtemp(prefix=f"eval-{scenario.id}-"))
    try:
        shutil.copy(template, work / "eval.db")
        logs_dir = work / "logs"
        logs_dir.mkdir()
        for service, text in scenario.logs.items():
            (logs_dir / f"{service}.log").write_text(text)
        database = CaseDatabase(work / "eval.db")
        case = database.create_case(CaseCreate(
            title=scenario.title, customer=scenario.customer, service=scenario.service,
            reported_issue=scenario.reported_issue, logs=scenario.attached_logs, severity=scenario.severity,
        ))
        box = InvestigationToolbox(case, database, embedder, NoJira(), logs_dir)
        calls = []
        callbacks = [UsageRecorder(calls.append)]
        started = time.perf_counter()
        error = ""
        cause = ""
        warnings: list[str] = []
        rejections = 0
        try:
            if mode == "single_shot":
                cause = OllamaSupportAnalyst(settings.ollama_url, settings.ollama_model).analyze(case).likely_cause
            elif mode == "agent":
                cause = run_investigation(box, settings.agent_model, settings.agent_max_steps,
                                          base_url=settings.ollama_url, callbacks=callbacks).analysis.likely_cause
            else:
                team = run_team(box, settings.agent_model, llm=llm, base_url=settings.ollama_url, callbacks=callbacks,
                                time_limit_seconds=settings.agent_time_limit_seconds or None)
                cause = team.analysis.likely_cause
                warnings = team.warnings
                rejections = sum(not r.approved for r in team.reviews)
        except Exception as exc:  # a failed run is a result, not a crash of the whole evaluation
            error = f"{type(exc).__name__}: {exc}"
        seconds = round(time.perf_counter() - started, 1)
        result = score(scenario, cause) if not error else {"verdict": "error", "copied_lookalike": False,
                                                           "cites_evidence": False}
        return {
            "scenario": scenario.id, "kind": scenario.kind, "mode": mode, **result,
            "checked_logs": any(s.tool == "read_service_logs" and s.arguments.get("service") == scenario.service
                                for s in box.steps),
            "seconds": seconds,
            "tool_calls": len(box.steps),
            "failed_tool_calls": sum(not s.ok for s in box.steps),
            "model_calls": len(calls),
            "tokens_in": sum(c.input_tokens for c in calls),
            "tokens_out": sum(c.output_tokens for c in calls),
            "likely_cause": cause[:600],
            "warnings": warnings,
            "reviewer_rejections": rejections,
            "error": error,
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


def summarize(results: list[dict]) -> dict:
    summary = {}
    for mode in MODES:
        rows = [r for r in results if r["mode"] == mode]
        if not rows:
            continue
        n = len(rows)
        look = [r for r in rows if r["kind"] == "lookalike"]
        unknown = [r for r in rows if r["kind"] == "no_evidence"]
        evidential = [r for r in rows if r["kind"] != "no_evidence" and r["verdict"] != "error"]
        summary[mode] = {
            "runs": n,
            "correct": sum(r["verdict"] == "correct" for r in rows),
            "partial": sum(r["verdict"] == "partial" for r in rows),
            "wrong": sum(r["verdict"] == "wrong" for r in rows),
            "errors": sum(r["verdict"] == "error" for r in rows),
            "copied_lookalike": f"{sum(r['copied_lookalike'] for r in look)}/{len(look)}" if look else "—",
            "said_unknown": f"{sum(r['verdict'] == 'correct' for r in unknown)}/{len(unknown)}" if unknown else "—",
            "cites_evidence": round(sum(r["cites_evidence"] for r in evidential) / len(evidential), 2) if evidential else None,
            "median_seconds": statistics.median(r["seconds"] for r in rows),
            "avg_tool_calls": round(statistics.fmean(r["tool_calls"] for r in rows), 1),
            "avg_tokens": round(statistics.fmean(r["tokens_in"] + r["tokens_out"] for r in rows)),
            "by_kind": {
                kind: f"{sum(r['verdict'] == 'correct' for r in rows if r['kind'] == kind)}/"
                      f"{sum(1 for r in rows if r['kind'] == kind)}"
                for kind in dict.fromkeys(r["kind"] for r in rows)
            },
        }
    return summary


def markdown(results: list[dict], meta: dict) -> str:
    summary = summarize(results)
    modes = [m for m in MODES if m in summary]
    lines = [
        f"# RootIQ agent evaluation ({meta['started'][:10]})",
        "",
        f"Model `{meta['model']}` through Ollama; {meta['scenarios']} scenarios; scored by a fixed rubric "
        "(see backend/evals/scenarios.py).",
        "",
        "| Metric | " + " | ".join(MODE_NAMES[m] for m in modes) + " |",
        "|---" * (len(modes) + 1) + "|",
    ]
    rows = [
        ("Correct root cause", lambda s: f"{s['correct']}/{s['runs']}"),
        ("Partly correct", lambda s: f"{s['partial']}/{s['runs']}"),
        ("Copied a look-alike past incident", lambda s: s["copied_lookalike"]),
        ("Said 'unknown' when there was no evidence", lambda s: s["said_unknown"]),
        ("Cites log evidence (timestamps)", lambda s: "—" if s["cites_evidence"] is None else f"{s['cites_evidence']:.0%}"),
        ("Median seconds per incident", lambda s: f"{s['median_seconds']:.0f}"),
        ("Tool calls per incident", lambda s: f"{s['avg_tool_calls']}"),
        ("Tokens per incident", lambda s: f"{s['avg_tokens']:,}" if s["avg_tokens"] else "—"),
        ("Errors", lambda s: f"{s['errors']}"),
    ]
    for label, fn in rows:
        lines.append(f"| {label} | " + " | ".join(fn(summary[m]) for m in modes) + " |")
    kinds = list(dict.fromkeys(r["kind"] for r in results))
    lines += ["", "## Correct by scenario type", "", "| Type | " + " | ".join(MODE_NAMES[m] for m in modes) + " |",
              "|---" * (len(modes) + 1) + "|"]
    for kind in kinds:
        lines.append(f"| {kind} | " + " | ".join(summary[m]["by_kind"].get(kind, "—") for m in modes) + " |")
    lines += ["", "## Every scenario", "", "| Scenario | Type | " + " | ".join(MODE_NAMES[m] for m in modes) + " |",
              "|---" * (len(modes) + 2) + "|"]
    for sid in dict.fromkeys(r["scenario"] for r in results):
        cells = []
        for m in modes:
            r = next((x for x in results if x["scenario"] == sid and x["mode"] == m), None)
            cells.append("—" if r is None else r["verdict"] + (" (copied look-alike)" if r["copied_lookalike"] else ""))
        kind = next(r["kind"] for r in results if r["scenario"] == sid)
        lines.append(f"| {sid} | {kind} | " + " | ".join(cells) + " |")
    lines += ["", "Scoring: a root cause is correct when it names every required element of the confirmed cause, "
              "partial when it names some. Substring matching can miss paraphrases and can be fooled by negations; "
              "see the JSON file for every answer."]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="RootIQ agent evaluation")
    parser.add_argument("--modes", default=",".join(MODES))
    parser.add_argument("--only", default="", help="comma-separated scenario ids")
    parser.add_argument("--resume", default="", help="continue a previous results JSON")
    parser.add_argument("--cloud", action="store_true", help="the agent team uses the app's model chains (Groq, Gemini, local)")
    parser.add_argument("--offline-memory", action="store_true", help="knowledge-base search without Ollama")
    parser.add_argument("--gate", action="store_true", help="fail (exit 2) when the team score drops below the baseline")
    parser.add_argument("--save-baseline", action="store_true", help="save this run's team score as the baseline")
    args = parser.parse_args()
    modes = [m for m in args.modes.split(",") if m in MODES]
    scenarios = [s for s in SCENARIOS if not args.only or s.id in args.only.split(",")]
    settings = Settings()
    embedder = OfflineEmbedder() if args.offline_memory else LocalEmbedder(settings.ollama_url, settings.embedding_model)
    llm = None
    if args.cloud:
        from ..app.agent.llm import make_chat, model_label

        llm = make_chat(settings)

    REPORTS.mkdir(exist_ok=True)
    out = Path(args.resume) if args.resume else REPORTS / f"evals-{datetime.now(UTC):%Y%m%d-%H%M}.json"
    state = json.loads(out.read_text()) if args.resume else {
        "meta": {"started": datetime.now(UTC).isoformat(), "model": model_label(settings) if args.cloud else settings.agent_model,
                 "scenarios": len(scenarios), "modes": modes},
        "results": [],
    }
    done = {(r["scenario"], r["mode"]) for r in state["results"]}

    with tempfile.TemporaryDirectory() as tmp:
        template = Path(tmp) / "template.db"
        db = CaseDatabase(template)
        db.initialize()
        seed(db, embedder)
        for case in [c for c in db.list_cases() if c.status != "resolved"]:  # keep only the resolved history
            db.delete_case(case.id)

        total = len(scenarios) * len(modes)
        for i, (scenario, mode) in enumerate(((s, m) for s in scenarios for m in modes), 1):
            if (scenario.id, mode) in done:
                continue
            result = run_one(scenario, mode, template, settings, embedder, llm=llm if mode == "team" else None)
            state["results"].append(result)
            out.write_text(json.dumps(state, indent=2))
            print(f"[{i}/{total}] {scenario.id:<22} {MODE_NAMES[mode]:<13} {result['verdict']:<8}"
                  f"{' copied-lookalike' if result['copied_lookalike'] else '':<17} {result['seconds']:>6.1f}s"
                  f"{'  ' + result['error'][:80] if result['error'] else ''}", flush=True)

    report = out.with_suffix(".md")
    report.write_text(markdown(state["results"], state["meta"]))
    print(f"\nResults: {out}\nReport:  {report}")
    score = team_score(state["results"])
    if args.save_baseline and score is not None:
        BASELINE.write_text(json.dumps({"team_score": score, "scenarios": [s.id for s in scenarios], "model": state["meta"]["model"],
                                        "saved": datetime.now(UTC).isoformat()}, indent=2) + "\n")
        print(f"Baseline saved: team score {score}.")
    if args.gate:
        baseline = json.loads(BASELINE.read_text()) if BASELINE.exists() else {"team_score": 0}
        ok, message = gate(score, baseline)
        print(("PASS " if ok else "FAIL ") + message)
        return 0 if ok else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
