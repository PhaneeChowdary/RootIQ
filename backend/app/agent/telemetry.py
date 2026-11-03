"""Observability for agent runs: the model calls of each agent, and metrics across runs.

UsageRecorder is a LangChain callback. Each model call in a run has the metadata {"agent": name}, which the team sets
where it calls the model. Thus the app counts each call for the agent that made it.
"""
import statistics
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

from ..models import AgentLesson, AgentMetrics, AgentRun, AgentUsage, LLMCall

AGENT_ORDER = ("lead", "evidence", "history", "reviewer", "comms", "finalize", "execute")


class UsageRecorder(BaseCallbackHandler):
    def __init__(self, sink: Callable[[LLMCall], None]) -> None:
        self.sink = sink
        self._open: dict[UUID, tuple[str, str, datetime, float]] = {}
        self._lock = threading.Lock()

    def on_chat_model_start(self, serialized, messages, *, run_id: UUID, metadata=None, **kwargs) -> None:
        agent = (metadata or {}).get("agent", "unattributed")
        model = (metadata or {}).get("ls_model_name", "")  # LangChain adds this for every chat model
        with self._lock:
            self._open[run_id] = (agent, model, datetime.now(UTC), time.perf_counter())

    def on_llm_end(self, response, *, run_id: UUID, **kwargs) -> None:
        usage = {}
        if response.generations and response.generations[0]:
            usage = getattr(response.generations[0][0], "message", None)
            usage = getattr(usage, "usage_metadata", None) or {}
        self._close(run_id, ok=True, input_tokens=usage.get("input_tokens", 0), output_tokens=usage.get("output_tokens", 0))

    def on_llm_error(self, error, *, run_id: UUID, **kwargs) -> None:
        text = str(error)
        short = "Rate limit or quota" if "429" in text or "RESOURCE_EXHAUSTED" in text else text.split("\n")[0][:160]
        self._close(run_id, ok=False, error=short)

    def _close(self, run_id: UUID, ok: bool, input_tokens: int = 0, output_tokens: int = 0, error: str = "") -> None:
        with self._lock:
            opened = self._open.pop(run_id, None)
        if opened is None:
            return
        agent, model, started_at, started = opened
        self.sink(LLMCall(agent=agent, started_at=started_at, duration_ms=round((time.perf_counter() - started) * 1000),
                          input_tokens=input_tokens, output_tokens=output_tokens, ok=ok, model=model, error=error))


def investigation_seconds(run: AgentRun) -> float | None:
    """Wall time from the start of the first agent to the final analysis, without the wait for approval."""
    finals = [s for s in run.spans if s.agent == "finalize"]
    if not run.spans or not finals:
        return None
    start = min(s.started_at for s in run.spans)
    end = max(s.started_at.timestamp() + s.duration_ms / 1000 for s in finals)
    return round(end - start.timestamp(), 1)


def compute_metrics(runs: list[AgentRun], lessons: list[AgentLesson]) -> AgentMetrics:
    finished = [r for r in runs if r.analysis is not None]
    durations = sorted(d for r in finished if (d := investigation_seconds(r)) is not None)
    with_usage = [r for r in finished if r.llm_calls]
    steps = [s for r in finished for s in r.steps]
    reviewed = [r for r in finished if r.reviews]

    per_agent: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for run in with_usage:
        for agent in {c.agent for c in run.llm_calls} | {s.agent for s in run.spans}:
            calls = [c for c in run.llm_calls if c.agent == agent]
            per_agent[agent]["seconds"].append(sum(s.duration_ms for s in run.spans if s.agent == agent) / 1000)
            per_agent[agent]["calls"].append(len(calls))
            per_agent[agent]["input"].append(sum(c.input_tokens for c in calls))
            per_agent[agent]["output"].append(sum(c.output_tokens for c in calls))

    def mean(values: list[float]) -> float | None:
        return round(statistics.fmean(values), 1) if values else None

    return AgentMetrics(
        runs_total=len(runs),
        by_status=dict(Counter(r.status for r in runs)),
        by_trigger=dict(Counter(r.trigger for r in runs)),
        median_investigation_seconds=round(statistics.median(durations), 1) if durations else None,
        p90_investigation_seconds=durations[min(len(durations) - 1, int(0.9 * len(durations)))] if durations else None,
        avg_input_tokens=mean([sum(c.input_tokens for c in r.llm_calls) for r in with_usage]),
        avg_output_tokens=mean([sum(c.output_tokens for c in r.llm_calls) for r in with_usage]),
        avg_tool_calls=mean([len(r.steps) for r in finished]),
        tool_failure_rate=round(sum(not s.ok for s in steps) / len(steps), 3) if steps else None,
        reviewer_rejection_rate=(
            round(sum(any(not v.approved for v in r.reviews) for r in reviewed) / len(reviewed), 3) if reviewed else None
        ),
        decisions=dict(Counter(o.decision for r in runs for o in r.outcomes if o.decision != "none")),
        lesson_verdicts=dict(Counter(lesson.verdict for lesson in lessons)),
        per_agent=[
            AgentUsage(agent=agent, runs=len(v["seconds"]), avg_seconds=mean(v["seconds"]) or 0.0,
                       avg_llm_calls=mean(v["calls"]) or 0.0, avg_input_tokens=mean(v["input"]) or 0.0,
                       avg_output_tokens=mean(v["output"]) or 0.0)
            for agent, v in sorted(per_agent.items(),
                                   key=lambda item: AGENT_ORDER.index(item[0]) if item[0] in AGENT_ORDER else 99)
        ],
    )
