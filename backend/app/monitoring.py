"""Prometheus metrics for Grafana.

GET /metrics gives the numbers in the Prometheus text format. Prometheus reads this page every 15 seconds.
Grafana shows the numbers on the "RootIQ Agents" dashboard and uses them in alert rules.

The collector reads the current data at each request. It does not keep a second copy of the data.
The metrics are in four groups:
- Cases: the number of cases by status and severity, and the SLA state of open cases.
- Agent runs: runs, queue, investigation time, model calls, tokens, tool calls, decisions and lesson grades.
- Models: the main model, and whether it is paused because of a quota.
- Services: the number of ERROR lines in each service log. A Grafana alert rule watches this number.
"""

from collections import Counter
from datetime import UTC, datetime, timedelta

from prometheus_client import CollectorRegistry
from prometheus_client import Counter as PromCounter
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, HistogramMetricFamily

from .agent import llm
from .agent.telemetry import investigation_seconds

BUCKETS = [60, 120, 180, 240, 300, 420, 600, 900]  # seconds: an investigation usually takes 2 to 5 minutes


class RootIQCollector:
    """Makes the metric families from the current data. The getters give the current objects. Tests replace them."""

    def __init__(self, get_settings, get_database, get_runner) -> None:
        self.get_settings = get_settings
        self.get_database = get_database
        self.get_runner = get_runner

    def describe(self):
        return []  # the families are made at collect time

    def collect(self):
        settings, database, runner = self.get_settings(), self.get_database(), self.get_runner()
        yield from self._cases(database)
        yield from self._runs(runner)
        yield from self._models(settings)
        yield from self._service_errors(settings)

    @staticmethod
    def _cases(database):
        cases = database.list_cases()
        by_status = GaugeMetricFamily("rootiq_cases", "Cases by status and severity.", labels=["status", "severity"])
        for (status, severity), n in Counter((c.status, c.severity) for c in cases).items():
            by_status.add_metric([status, severity], n)
        sla = GaugeMetricFamily("rootiq_open_cases_sla", "Open cases by SLA state.", labels=["state"])
        open_states = Counter(c.sla_state for c in cases if c.status != "resolved")
        for state in ("on_track", "at_risk", "breached"):
            sla.add_metric([state], open_states.get(state, 0))
        yield by_status
        yield sla

    @staticmethod
    def _runs(runner):
        runs = runner.store.list_all()
        total = CounterMetricFamily("rootiq_agent_runs", "Agent runs by status and trigger.", labels=["status", "trigger"])
        for (status, trigger), n in Counter((r.status, r.trigger) for r in runs).items():
            total.add_metric([status, trigger], n)
        yield total

        queued = sum(1 for r in runs if r.status == "queued")
        yield GaugeMetricFamily("rootiq_agent_queue_length", "Runs that wait in the queue.", value=queued)
        workers = runner.store.running_jobs()
        yield GaugeMetricFamily("rootiq_agent_jobs_running", "Jobs that workers run now (all processes).", value=len(workers))

        durations = [d for r in runs if (d := investigation_seconds(r)) is not None]
        buckets = [(str(b), sum(1 for d in durations if d <= b)) for b in BUCKETS] + [("+Inf", len(durations))]
        yield HistogramMetricFamily("rootiq_agent_investigation_seconds", "Time from the first agent to the final analysis.",
                                    buckets=buckets, sum_value=sum(durations))

        calls = CounterMetricFamily("rootiq_agent_model_calls", "Model calls by agent, model and outcome.",
                                    labels=["agent", "model", "outcome"])
        tokens = CounterMetricFamily("rootiq_agent_tokens", "Tokens by agent and direction.", labels=["agent", "direction"])
        call_counts, token_counts = Counter(), Counter()
        for run in runs:
            for c in run.llm_calls:
                call_counts[(c.agent, c.model or "not recorded", "ok" if c.ok else "failed")] += 1
                token_counts[(c.agent, "input")] += c.input_tokens
                token_counts[(c.agent, "output")] += c.output_tokens
        for labels, n in call_counts.items():
            calls.add_metric(list(labels), n)
        for labels, n in token_counts.items():
            tokens.add_metric(list(labels), n)
        yield calls
        yield tokens

        tools = CounterMetricFamily("rootiq_agent_tool_calls", "Tool calls by agent, tool and outcome.", labels=["agent", "tool", "outcome"])
        for (agent, tool, ok), n in Counter((s.agent, s.tool, s.ok) for r in runs for s in r.steps).items():
            tools.add_metric([agent, tool, "ok" if ok else "failed"], n)
        yield tools

        warned = CounterMetricFamily("rootiq_agent_runs_with_warnings", "Runs whose analysis has a guard warning.")
        warned.add_metric([], sum(1 for r in runs if r.warnings))
        yield warned

        metrics = runner.metrics()
        decisions = CounterMetricFamily("rootiq_agent_decisions", "Engineer decisions on proposals.", labels=["decision"])
        for decision, n in metrics.decisions.items():
            decisions.add_metric([decision], n)
        verdicts = CounterMetricFamily("rootiq_agent_lessons", "Diagnoses graded after resolution.", labels=["verdict"])
        for verdict, n in metrics.lesson_verdicts.items():
            verdicts.add_metric([verdict], n)
        yield decisions
        yield verdicts

    @staticmethod
    def _models(settings):
        provider, model = llm.active_model(settings)
        info = GaugeMetricFamily("rootiq_main_model", "The main model (value 1).", labels=["provider", "model"])
        info.add_metric([provider, model], 1)
        paused = GaugeMetricFamily("rootiq_main_model_paused", "1 when the main model is paused because of a quota.")
        paused.add_metric([], 1 if provider != "ollama" and llm.pause_reason(provider, model) else 0)
        yield info
        yield paused

    @staticmethod
    def _service_errors(settings):
        """ERROR lines in each service log: all of them, and those of the last 5 minutes by the time on each line.

        The alert rule uses the 5-minute count. It is an exact whole number, and a log that simulate_incident --reset puts
        back cannot look like a burst of new errors.
        """
        total = CounterMetricFamily("rootiq_service_log_errors", "ERROR lines in each service log.", labels=["service"])
        recent = GaugeMetricFamily("rootiq_service_log_errors_5m", "ERROR lines of the last 5 minutes in each service log.",
                                   labels=["service"])
        since = datetime.now(UTC) - timedelta(minutes=5)
        for path in sorted(settings.logs_dir.glob("*.log")):
            errors = [line for line in path.read_text().splitlines() if " ERROR " in line]
            total.add_metric([path.stem], len(errors))
            recent.add_metric([path.stem], sum(1 for line in errors if (t := _line_time(line)) and t >= since))
        return [total, recent]


def _line_time(line: str) -> datetime | None:
    """The time at the start of a log line, for example 2026-10-03T19:53:02Z."""
    try:
        return datetime.fromisoformat(line.split(" ", 1)[0].replace("Z", "+00:00"))
    except ValueError:
        return None


def build_registry(get_settings, get_database, get_runner) -> tuple[CollectorRegistry, PromCounter]:
    """The registry for /metrics and the counter of alerts that Grafana sent."""
    from prometheus_client import disable_created_metrics

    disable_created_metrics()  # no extra "_created" series: Grafana does not use them
    registry = CollectorRegistry()
    registry.register(RootIQCollector(get_settings, get_database, get_runner))
    alerts = PromCounter("rootiq_alerts_received", "Alerts received from Grafana, by status and result.",
                         ["status", "result"], registry=registry)
    return registry, alerts
