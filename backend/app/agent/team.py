"""Multi-agent incident investigation team, built with LangGraph.

    START -> lead --> evidence --\\
                 |--> history  ---> reviewer --approved--> [remediation] -> comms -> finalize --> END
                 |--> [changes] -/        |                                                    \\
                 \\--> [code]    -/        |                                               with approval
                         ^                |                                                       |
                         \\--rejected: the named specialists run again, one time    approval -> execute -> END

lead         plans: the hypotheses and a task for each specialist, as structured output
evidence     examines the service logs, metrics and database with tools           } these agents run
history      examines resolved past incidents and Jira with tools                 } at the same time
changes      finds the deploys, config changes and commits just before the start } when a change source is on
code         reads the code of the service                                        } when a code source is on
reviewer     checks each claim against the raw tool output, not against the summaries of the specialists
remediation  proposes at most two fixes from the catalog of automation.py, only when the evidence supports them
             and remediation is on. The code checks each rule, and a person approves before a fix runs.
comms        records the customer update and the Jira comment as proposals. It sends nothing.
finalize     writes the SupportAnalysis. The team can raise the severity but never lowers it.
approval     pauses the run with a LangGraph interrupt until an engineer approves, edits or rejects each proposal.
             The checkpointer keeps the paused state, so the run continues after a server restart.
execute      carries out only the approved actions, through the executor. The executor can be in dry-run mode.
"""
import json
import operator
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt

from ..models import (
    ActionDecision,
    ActionOutcome,
    AgentSpan,
    InvestigationPlan,
    ProposedAction,
    ReviewVerdict,
    SupportAnalysis,
    TeamRunResult,
)
from .graph import describe_case
from .guards import Deadline, misquoted_code, misquoted_lines, unverified_code_refs, unverified_ids, unverified_log_times, without_code
from .llm import ollama_chat
from .tools import InvestigationToolbox, build_tools

SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
MAX_REVISIONS = 1
SPECIALIST_ROUNDS = 5
RAW_EVIDENCE_CHARS = 12000
# Tools that return code. A list of file names is not enough to cite code.
CODE_READ_TOOLS = {"search_code", "read_code", "find_symbol"}

LEAD_PROMPT = """You lead a production incident investigation team. Read the incident and write a plan:
2-3 hypotheses for the cause, one task for the evidence agent (who reads service logs and metrics), one task for the
history agent (who searches resolved past incidents and Jira), one task for the changes agent (who reads recent
deploys, config changes and commits) and one task for the code agent (who reads the service's code). Each task must
name concrete things to check.
If lessons from past investigations are listed, they were checked against real resolutions: let them shape the tasks."""

SPECIALISTS = {
    "evidence": {
        "tools": {"read_service_logs", "list_metrics", "query_metrics", "db_activity", "db_schema", "db_query"},
        "prompt": """You are the evidence agent on an incident team. You read server logs.
First read the affected service's logs with no filter to see the sequence of events around the incident.
Then narrow with short filters (an error code, a component, an id). For each relevant line, note the component
and code path it comes from (the [bracketed] component and fields such as path=). Quote exact lines with timestamps.
If you have list_metrics and query_metrics, check how the error rate or latency changed and when. If you have
db_activity, check the service database for waiting or blocked connections and long statements; db_query only reads.""",
    },
    "code": {
        "tools": {"list_code_files", "search_code", "read_code", "find_symbol", "propose_code_change"},
        "prompt": """You are the code agent on an incident team. You read the service's code; you cannot change it.
Follow the evidence into the code: search for the log tags, error texts and config keys of the incident (for example
"gateway-client", "pool exhausted", "max_size"), then read the files around the hits. Report each finding as
path:line with what that code does and how it explains the incident. Cite only lines that you read with
read_code or search_code, and copy quoted code exactly: a file name from list_code_files is not evidence.
Write a patch only with propose_code_change, never as text. If the cause is in the code or its configuration, call propose_code_change one time with the smallest change: copy
old_code exactly from read_code, without the line numbers. The patch is for a person to review; it is never applied
by the app. If the code does not explain the incident, say so.""",
    },
    "changes": {
        "tools": {"recent_changes"},
        "prompt": """You are the changes agent on an incident team. You read recent deploys, config changes and
commits with recent_changes. Find the first error time in the case or the evidence, then the changes just before it.
For each relevant change give its exact time, its id or release, what it changed, and the minutes between the change
and the first error. Name the release that ran before a bad release (a rollback target) only if a tool shows it.
Do not name code files or functions: the code agent reads the code. Name only changes that recent_changes returned.
A change long before the errors, or after them, is not the cause: say so.""",
    },
    "history": {
        "tools": {"search_similar_incidents", "get_incident", "list_service_incidents", "search_jira"},
        "prompt": """You are the history agent on an incident team. You find resolved past incidents and Jira
tickets that match the current one. Search, then open the closest matches with get_incident. State how the current
incident is similar AND how it differs from each match. Cite incident ids.""",
    },
}

PATCH_PROMPT = """Write the patch now. If the code above explains the incident, call propose_code_change one time, with
the smallest fix at the place where the bad value comes from. Copy old_code from the code above. If the code above
does not explain the incident, do not call a tool: answer NO_PATCH and say why in one sentence.

How to fill propose_code_change, for example when read_code showed
   41  def ratio(a, b):
   42      return a / b
- start_line 42, end_line 42
- old_code: the current lines, copied exactly, without the numbers:     return a / b
- new_code: the lines that replace them, with the fix:     return a / b if b else 0.0
- reason: the cause with its evidence, for example "app.py:42 divides by b, and the 14:02:10Z ERROR line shows b=0"."""

REPORT_PROMPT = """Write your findings report now, without calling tools. Use short bullets. Every bullet must cite
its evidence: a quoted log line with its timestamp, or an incident id. Mark anything you could not confirm as unconfirmed."""

REVIEW_PROMPT = """You review an incident investigation. Compare every claim in the specialists' findings with the
RAW TOOL OUTPUT. A claim is supported only if the raw output shows it. Reject findings that name a component, code
path or cause the raw output does not show, or that copy a past incident's cause without current evidence.
unsupported_claims lists only statements that appear in the findings and that the raw output contradicts or does
not show; information that is simply missing is not an unsupported claim. If you reject, name the specialists
that must recheck and say exactly what to check. Approve only when unsupported_claims is empty."""

COMMS_PROMPT = """You are the communications agent on an incident team. Using only the reviewed findings, call
propose_customer_update with a short, honest status update for the customer: no claim that the issue is fixed and
no internal hostnames, ids or log lines. If propose_jira_comment is available, also call it with an internal summary
that cites the evidence. Write plain text in both: no Markdown, no "#" headings and no "*" or "**" marks. Use "- " for
a list. Then stop."""

REMEDIATION_PROMPT = """You are the remediation agent on an incident team. Read the reviewed findings. If the
evidence clearly shows the cause AND an action from the catalog removes it, call propose_remediation one time.
Choose the smallest safe action that addresses the cause: roll back the release that caused it, revert the config
change that caused it (revert_config_change with its change id), or turn off the flag that caused it. A restart or more instances do not fix a bad release, a bad config or a broken
certificate. Use only versions and flags that a tool returned. Quote the evidence with times in the reason.
If the cause is not clear, or no catalog action fixes it, do not call the tool: write one sentence that says why."""

FINAL_PROMPT = """Write the final analysis for the support engineer as JSON matching the schema, using only the
reviewed findings. Fill every field. incident_summary: two sentences on what is happening. customer_impact: who is
affected and how. likely_cause: cite the specific evidence (log lines with timestamps, incident ids) and name the
component involved. diagnostic_steps: 3 to 5 short checks for the engineer, each based on the evidence.
information_to_request: what to ask the customer, or an empty list. workaround: from the evidence or past incidents,
or say that none is known. Quote only log lines that appear in the findings, with their exact timestamps."""


class _LoopState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    rounds: int


def tagged(agent: str) -> dict:
    """Run config that names the agent of each model call, for the telemetry callback."""
    return {"metadata": {"agent": agent}}


def patch_brief(task: str, messages: list[AnyMessage], limit: int = 8_000) -> str:
    """The patch turn's input: the task, what the code agent concluded, and the code it read (newest first, at most
    `limit` characters). It is enough to write the patch, and much smaller than the whole conversation."""
    code = []
    size = 0
    for message in reversed(messages):
        if isinstance(message, ToolMessage) and message.name in ("read_code", "find_symbol", "search_code"):
            text = str(message.content)
            if size + len(text) > limit:
                continue
            code.append(f"[{message.name}] {text}")
            size += len(text)
    said = next((m.text for m in reversed(messages) if isinstance(m, AIMessage) and (m.text or "").strip()), "")
    return (f"{task}\n\nWhat you found:\n{said[:2000] or '(no notes)'}\n\nCode that you read (real lines with numbers):\n"
            + "\n\n".join(reversed(code)) + f"\n\n{PATCH_PROMPT}")


def run_tool_loop(llm, tools, system: str, task: str, max_rounds: int, agent: str = "agent",
                  deadline: Deadline | None = None, history: list[AnyMessage] | None = None) -> list[AnyMessage]:
    """One agent's ReAct loop: the model calls tools and sees results until it stops, hits max_rounds,
    or the run's time budget runs out. With history, the loop continues an earlier conversation."""
    planner = llm.bind_tools(tools)

    def think(state: _LoopState) -> dict:
        if deadline and deadline.expired:
            return {"messages": [AIMessage(content="Stopped: the run's time limit was reached.")]}
        response = planner.invoke([SystemMessage(system), *state["messages"]])
        return {"messages": [response], "rounds": state["rounds"] + (1 if response.tool_calls else 0)}

    def route(state: _LoopState) -> str:
        last = state["messages"][-1]
        return "tools" if last.tool_calls and state["rounds"] <= max_rounds else END

    loop = StateGraph(_LoopState)
    loop.add_node("agent", think)
    loop.add_node("tools", ToolNode(tools, handle_tool_errors=True))
    loop.add_edge(START, "agent")
    loop.add_conditional_edges("agent", route, ["tools", END])
    loop.add_edge("tools", "agent")
    # each call is a new loop. It must not use the checkpointer of the parent: else a second call continues the first.
    out = loop.compile(checkpointer=False).invoke(
        {"messages": [*(history or []), HumanMessage(task)], "rounds": 0}, {"recursion_limit": 2 * max_rounds + 5, **tagged(agent)}
    )
    messages = out["messages"]
    if isinstance(messages[-1], AIMessage) and messages[-1].tool_calls:
        messages = messages[:-1]  # calls past the round limit were never run
    return messages


def _merge(left: dict[str, str], right: dict[str, str]) -> dict[str, str]:
    return {**left, **right}


class TeamState(TypedDict):
    plan: InvestigationPlan | None
    findings: Annotated[dict[str, str], _merge]
    reviews: Annotated[list[ReviewVerdict], operator.add]
    analysis: SupportAnalysis | None
    severity_note: str
    warnings: list[str]
    proposals: list[ProposedAction]
    decisions: list[ActionDecision]
    outcomes: list[ActionOutcome]


Executor = Callable[[list[ProposedAction], list[ActionDecision]], list[ActionOutcome]]


def initial_state() -> TeamState:
    return {"plan": None, "findings": {}, "reviews": [], "analysis": None, "severity_note": "", "warnings": [],
            "proposals": [], "decisions": [], "outcomes": []}


def build_team(
    llm,
    box: InvestigationToolbox,
    specialist_rounds: int = SPECIALIST_ROUNDS,
    executor: Executor | None = None,
    checkpointer=None,
    lessons: list[str] = (),
    on_span: Callable[[AgentSpan], None] | None = None,
    deadline: Deadline | None = None,
    policy: dict | None = None,
):
    """Without an executor, the team stops after finalize: the CLI and the evals use this. With an executor, it
    pauses for approval. on_span receives the wall time of each agent step; a node that runs two times gives two spans.
    After the deadline, the agents stop calling tools, the app skips the review and comms steps, and the final analysis
    uses what the agents found, with a warning."""
    case_text = describe_case(box.case)
    deadline = deadline or Deadline(None)
    active = ["evidence", "history"] + (["changes"] if box.changes is not None else []) + (["code"] if box.code is not None else [])

    def findings_text(findings: dict[str, str]) -> str:
        """The findings for the other agents. Code stays on this computer unless the code source allows cloud models."""
        return "\n\n".join(f"## {name} findings\n{without_code(text) if name == 'code' and not box.code_cloud else text}"
                             for name, text in sorted(findings.items()))

    def chat(agent: str):
        """The model chain of the task level of this agent, see llm.ModelRouter, or the one model that tests and the CLI give."""
        return llm.for_agent(agent) if hasattr(llm, "for_agent") else llm

    def known_text() -> str:
        """Where an identifier may legitimately come from: the case, lessons, and results of tool calls that
        succeeded. Not the model's own tool arguments, and not error messages, which echo those arguments back."""
        results = "\n".join(box.evidence)
        return "\n".join([case_text, *lessons, results])

    def lead(state: TeamState) -> dict:
        briefing = case_text
        if lessons:
            briefing += "\n\nLessons from past investigations:\n" + "\n".join(f"- {item}" for item in lessons)
        plan = chat("lead").with_structured_output(InvestigationPlan).invoke(
            [SystemMessage(LEAD_PROMPT), HumanMessage(briefing)], config=tagged("lead")
        )
        return {"plan": plan}

    def specialist(name: str):
        spec = SPECIALISTS[name]

        def node(state: TeamState) -> dict:
            if deadline.expired:
                return {"findings": {name: state["findings"].get(name, "Not investigated: the time limit was reached.")}}
            task = getattr(state["plan"], f"{name}_task", "") or {
                "changes": "Find the deploys, config changes and commits just before the first error.",
                "code": "Find the code and configuration that produce the failing log lines, and the cause in them.",
            }.get(name, "")
            feedback = ""
            if state["reviews"] and not state["reviews"][-1].approved:
                verdict = state["reviews"][-1]
                feedback = (
                    f"\n\nThe reviewer rejected your earlier findings:\n{state['findings'].get(name, '')}\n"
                    f"Unsupported claims: {'; '.join(verdict.unsupported_claims) or 'not listed'}\n"
                    f"Check now: {verdict.instructions}"
                )
            if name == "code" and (leads := box.code_leads()):
                task = f"{task}\n\n{leads}"
            tools = build_tools(box, agent=name, only=spec["tools"])
            messages = run_tool_loop(
                chat(name), tools, spec["prompt"], f"{case_text}\n\nYour task from the lead: {task}{feedback}", specialist_rounds,
                agent=name, deadline=deadline,
            )
            read_code = lambda: any(s.agent == "code" and s.ok and s.tool in CODE_READ_TOOLS for s in box.steps)  # noqa: E731
            model = chat(name)
            if name == "code" and not read_code() and len(getattr(model, "links", [])) > 1 and not deadline.expired:
                # the first model did not use the tools: the next model of the chain does the step again
                from .llm import ChainChat

                messages = run_tool_loop(
                    ChainChat(model.links[1:], model.label), tools, spec["prompt"],
                    f"{case_text}\n\nYour task from the lead: {task}{feedback}", specialist_rounds, agent=name, deadline=deadline,
                )
            if name == "code" and not read_code():
                # the agent read no code, so a report would be invented. A model that cannot call tools writes a report anyway.
                return {"findings": {name: "The code agent read no code, so it has no code findings."}}
            if (name == "code" and not any(p.kind == "code_change" for p in box.proposals) and not deadline.expired
                    and not box.__dict__.get("_patch_turn_done")):
                # The patch turn: the first model of the code chain (with the Cloud models switch, a cloud model such as
                # OpenAI) writes the patch from a short brief: the task and the code that was read, not the whole
                # conversation. Thus a paid model gets a few small calls per run.
                # One turn per run, also when the reviewer sends the code agent back, and no reading in it: the brief
                # has the code. At most 3 small calls: the patch, one more try if the app refuses it, and the last reply.
                box.__dict__["_patch_turn_done"] = True
                patch_tools = build_tools(box, agent=name, only={"propose_code_change"})
                run_tool_loop(chat("code_patch"), patch_tools, spec["prompt"],
                              patch_brief(f"{case_text}\n\nYour task from the lead: {task}", messages), 2,
                              agent="code_patch", deadline=deadline)
            report = chat(name).invoke([SystemMessage(spec["prompt"]), *messages, HumanMessage(REPORT_PROMPT)], config=tagged(name))
            return {"findings": {name: (report.text or "").strip() or "No findings."}}

        return node

    def review(state: TeamState) -> dict:
        raw = "\n".join(
            f"[{s.agent}] {s.tool}({json.dumps(s.arguments, default=str)}) -> "
            + ("[code read; checked in code, kept on this computer]" if s.agent == "code" and not box.code_cloud else s.result)
            for s in box.steps
            if s.agent in SPECIALISTS
        )[-RAW_EVIDENCE_CHARS:]
        findings = findings_text(state["findings"])
        known = known_text()
        invented = [(name, i) for name, text in sorted(state["findings"].items()) for i in unverified_ids(text, known)]
        invented_claims = [f"The {name} findings cite {i}, which no tool returned." for name, i in invented]
        # A quoted log line must come from the logs. Its timestamp shows it: the tool output has each real one.
        made_up = [(name, t) for name, text in sorted(state["findings"].items()) for t in unverified_log_times(text, known)]
        invented_claims += [f"The {name} findings quote a log line at {t}, but no log has that time." for name, t in made_up]
        invented += made_up
        misquoted = [(name, q) for name, text in sorted(state["findings"].items()) for q in misquoted_lines(text, known)]
        invented_claims += [f"The {name} findings misquote the log line at {q}. Copy log lines exactly." for name, q in misquoted]
        invented += misquoted
        bad_refs = [(name, r) for name, text in sorted(state["findings"].items()) for r in unverified_code_refs(text, box.code_line_exists)]
        invented_claims += [f"The {name} findings cite {r}, but that file or line does not exist." for name, r in bad_refs]
        invented += bad_refs
        bad_quotes = [(name, q) for name, text in sorted(state["findings"].items()) for q in misquoted_code(text, box.code_lines)]
        invented_claims += [f"The {name} findings quote code at {q}. Copy code exactly from read_code." for name, q in bad_quotes]
        invented += bad_quotes
        if deadline.expired:
            return {"reviews": [ReviewVerdict(approved=False, unsupported_claims=invented_claims, recheck=[],
                                              instructions="Not reviewed: the run's time limit was reached.")]}
        verdict = chat("reviewer").with_structured_output(ReviewVerdict).invoke(
            [SystemMessage(REVIEW_PROMPT), HumanMessage(f"{case_text}\n\n{findings}\n\nRAW TOOL OUTPUT:\n{raw}")],
            config=tagged("reviewer"),
        )
        if invented:  # the code checks this, not the model: an identifier that no tool returned is invented
            ids = ", ".join(dict.fromkeys(i for _, i in invented))
            verdict = verdict.model_copy(update={
                "approved": False,
                "unsupported_claims": [*verdict.unsupported_claims, *invented_claims],
                "recheck": list(dict.fromkeys([*verdict.recheck, *(name for name, _ in invented)])),
                "instructions": f"{verdict.instructions} Remove or verify {ids}: quote only identifiers and log lines that a tool returned. Read the logs again with no filter.".strip(),
            })
        if verdict.approved and verdict.unsupported_claims:
            verdict = verdict.model_copy(update={"approved": False})  # an approval that lists problems is not one
        recheck = [name for name in verdict.recheck if name in active]
        if not verdict.approved and not recheck:
            recheck = list(active)
        verdict = verdict.model_copy(update={"recheck": recheck})
        return {"reviews": [verdict]}

    after_reviewed = "remediation" if box.remediation is not None else "comms"

    def after_review(state: TeamState) -> list[str]:
        last = state["reviews"][-1]
        if last.approved or len(state["reviews"]) > MAX_REVISIONS or deadline.expired:
            return [after_reviewed]
        return list(dict.fromkeys(last.recheck))

    def remediation(state: TeamState) -> dict:
        if deadline.expired or not state["reviews"] or not state["reviews"][-1].approved:
            return {}  # no change to a running service from findings that the reviewer did not approve
        findings = findings_text(state["findings"])
        tools = build_tools(box, agent="remediation", only={"propose_remediation"})
        run_tool_loop(chat("remediation"), tools, REMEDIATION_PROMPT, f"{case_text}\n\nReviewed findings:\n{findings}",
                      max_rounds=3, agent="remediation", deadline=deadline)
        return {}

    def comms(state: TeamState) -> dict:
        if deadline.expired:
            return {}  # out of time: no customer-facing proposals from partial work
        findings = findings_text(state["findings"])
        offered = {"propose_customer_update", "propose_jira_comment"} if box.has_customer else {"propose_jira_comment"}
        tools = build_tools(box, agent="comms", only=offered)
        task = f"{case_text}\n\nReviewed findings:\n{findings}"
        run_tool_loop(chat("comms"), tools, COMMS_PROMPT, task, max_rounds=3, agent="comms", deadline=deadline)
        if (missing := box.missing_required_actions()) and not deadline.expired:
            run_tool_loop(chat("comms"), tools, COMMS_PROMPT, f"{task}\n\nYou must call: {', '.join(missing)}.", max_rounds=3,
                          agent="comms", deadline=deadline)
        return {}

    def finalize(state: TeamState) -> dict:
        findings = findings_text(state["findings"])
        writer = chat("finalize").with_structured_output(SupportAnalysis)
        prompt = [SystemMessage(FINAL_PROMPT), HumanMessage(f"{case_text}\n\n{findings}")]
        analysis = writer.invoke(prompt, config=tagged("finalize"))
        warnings: list[str] = []
        known = known_text()

        def invented_in(a: SupportAnalysis) -> list[str]:
            text = " ".join([a.incident_summary, a.likely_cause, a.workaround, a.customer_email_body, *a.diagnostic_steps])
            return (unverified_ids(text, known) + [f"a log line at {t}" for t in unverified_log_times(text, known)]
                    + [f"a misquoted log line at {q}" for q in misquoted_lines(text, known)]
                    + [f"the code reference {r}" for r in unverified_code_refs(text, box.code_line_exists)]
                    + [f"the code quote at {q}" for q in misquoted_code(text, box.code_lines)])

        if (bad := invented_in(analysis)) and not deadline.expired:
            analysis = writer.invoke([*prompt, HumanMessage(
                f"{', '.join(bad)} is not in the evidence: no tool returned it. Rewrite the analysis without it."
            )], config=tagged("finalize"))
            bad = invented_in(analysis)
        if bad:
            warnings.append(f"Unverified evidence in the analysis: {', '.join(bad)}. No tool returned it.")
        if deadline.expired:
            warnings.append(f"Time limit of {deadline.seconds:.0f} s reached: the analysis uses partial evidence, "
                            "the review was skipped and no customer-facing proposals were made.")
        update: dict = {}
        if not analysis.workaround.strip():
            update["workaround"] = "No workaround is known yet. Use the diagnostic steps to confirm the cause first."
        note = ""
        reported = box.case.severity
        if SEVERITY_RANK[analysis.suggested_severity] < SEVERITY_RANK[reported]:
            note = (f"Team suggested {analysis.suggested_severity}; kept the reported {reported}. "
                    "Agents may raise severity but only an engineer can lower it.")
            update["suggested_severity"] = reported
        elif SEVERITY_RANK[analysis.suggested_severity] > SEVERITY_RANK[reported]:
            note = f"Team raised the severity from {reported} to {analysis.suggested_severity}. Check it before you approve."
        update_mail = next((p for p in box.proposals if p.kind == "customer_update"), None)
        if update_mail:
            update["customer_email_subject"] = update_mail.payload["subject"]
            update["customer_email_body"] = update_mail.payload["body"]
        proposals = list(box.proposals)
        if state["reviews"] and not state["reviews"][-1].approved and any(p.kind == "code_change" for p in proposals):
            proposals = [p for p in proposals if p.kind != "code_change"]  # no patch from findings the reviewer did not approve
            warnings.append("The code patch was removed: the reviewer did not approve the findings.")
        return {"analysis": analysis.model_copy(update=update), "severity_note": note, "warnings": warnings,
                "proposals": [with_policy(p) for p in proposals]}

    def with_policy(proposal: ProposedAction) -> ProposedAction:
        from ..automation import RISK, approvals_needed

        key = proposal.payload.get("action", "") if proposal.kind == "remediation" else proposal.kind
        needed = approvals_needed(policy, key) if policy else 1
        return proposal.model_copy(update={"risk": RISK.get(key, proposal.risk), "approvals_needed": needed})

    def approval(state: TeamState) -> dict:
        if not state["proposals"]:
            return {"decisions": []}
        automatic = [ActionDecision(index=i, action="approve") for i, p in enumerate(state["proposals"]) if p.approvals_needed == 0]
        if len(automatic) == len(state["proposals"]):
            return {"decisions": automatic}  # only low-risk actions that the policy runs without a person
        reply = interrupt({"proposals": [p.model_dump() for p in state["proposals"]]})
        return {"decisions": [ActionDecision.model_validate(d) for d in reply]}

    def execute(state: TeamState) -> dict:
        return {"outcomes": executor(state["proposals"], state["decisions"])}

    def timed(agent: str, fn):
        if on_span is None:
            return fn

        def node(state: TeamState) -> dict:
            started_at, started = datetime.now(UTC), time.perf_counter()
            result = fn(state)
            on_span(AgentSpan(agent=agent, started_at=started_at, duration_ms=round((time.perf_counter() - started) * 1000)))
            return result

        return node

    team = StateGraph(TeamState)
    team.add_node("lead", timed("lead", lead))
    team.add_node("evidence", timed("evidence", specialist("evidence")))
    team.add_node("history", timed("history", specialist("history")))
    if "changes" in active:
        team.add_node("changes", timed("changes", specialist("changes")))
    if "code" in active:
        team.add_node("code", timed("code", specialist("code")))
    team.add_node("review", timed("reviewer", review))
    if box.remediation is not None:
        team.add_node("remediation", timed("remediation", remediation))
    team.add_node("comms", timed("comms", comms))
    team.add_node("finalize", timed("finalize", finalize))
    team.add_edge(START, "lead")
    team.add_edge("lead", "evidence")
    team.add_edge("lead", "history")
    team.add_edge("evidence", "review")
    team.add_edge("history", "review")
    if "changes" in active:
        team.add_edge("lead", "changes")
        team.add_edge("changes", "review")
    if "code" in active:
        team.add_edge("lead", "code")
        team.add_edge("code", "review")
    team.add_conditional_edges("review", after_review, [*active, after_reviewed])
    if box.remediation is not None:
        team.add_edge("remediation", "comms")
    team.add_edge("comms", "finalize")
    if executor is None:
        team.add_edge("finalize", END)
        return team.compile()
    team.add_node("approval", approval)
    team.add_node("execute", timed("execute", execute))
    team.add_edge("finalize", "approval")
    team.add_edge("approval", "execute")
    team.add_edge("execute", END)
    return team.compile(checkpointer=checkpointer)


def run_team(
    box: InvestigationToolbox,
    model: str,
    llm=None,
    base_url: str = "http://127.0.0.1:11434",
    specialist_rounds: int = SPECIALIST_ROUNDS,
    callbacks: list | None = None,
    on_span: Callable[[AgentSpan], None] | None = None,
    time_limit_seconds: float | None = None,
) -> TeamRunResult:
    llm = llm or ollama_chat(model, base_url)
    final = build_team(llm, box, specialist_rounds, on_span=on_span, deadline=Deadline(time_limit_seconds)).invoke(
        initial_state(), {"recursion_limit": 30, "callbacks": callbacks or []}
    )
    return TeamRunResult(
        case_id=box.case.id,
        model=model,
        plan=final["plan"],
        findings=final["findings"],
        reviews=final["reviews"],
        analysis=final["analysis"],
        steps=box.steps,
        proposals=box.proposals,
        severity_note=final["severity_note"],
        warnings=final["warnings"],
    )
