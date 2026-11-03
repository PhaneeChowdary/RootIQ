"""The investigation agent: a LangGraph loop of plan -> call tools -> observe, then a final report.

    START -> agent --tool calls, under the step limit--> tools -> agent
                   \\--stops early, required proposals missing--> remind -> agent   one time
                   \\--done, or step limit reached--> finalize -> END

The planner model sees the tools and decides what to call. The finalize step asks the writer model for a
SupportAnalysis. The analysis uses only what the tools returned.
"""
from collections.abc import Callable
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from ..models import AgentRunResult, SupportAnalysis, SupportCase
from .tools import InvestigationToolbox, build_tools

SYSTEM_PROMPT = """You are a senior production support engineer investigating a customer incident.
Work step by step with the tools. A good investigation usually:
1. Searches resolved past incidents for similar symptoms, and opens the closest one if it looks relevant.
2. Reads the service logs, filtering on errors or on terms from the report, to find direct evidence.
3. Checks other incidents on the same service to see whether this is a recurring problem.
4. Before finishing, always calls propose_customer_update, and propose_jira_comment when that tool is available.
Base every conclusion on tool results. If evidence is missing, say what is unknown instead of guessing.
Call one or two tools at a time. Once the likely cause is supported by evidence and the proposals are recorded, stop
calling tools and reply with a one-paragraph summary of your findings."""

FINALIZE_PROMPT = """Stop investigating. Write the final analysis for the support engineer as JSON matching the schema.
Use only evidence from the tool results above. In likely_cause, name the specific evidence (log lines, past incident ids).
Keep the current severity unless the evidence shows lower customer impact than reported.
The customer email must not claim the issue is fixed."""


class InvestigationState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    tool_rounds: int
    reminded: bool
    analysis: SupportAnalysis | None


def describe_case(case: SupportCase) -> str:
    jira_key = case.jira_source_key or case.jira_issue_key or "none"
    return (
        f"Investigate this incident.\n"
        f"Title: {case.title}\nCustomer: {case.customer}\nService: {case.service}\n"
        f"Environment: {case.environment}\nCurrent severity: {case.severity}\nJira ticket: {jira_key}\n"
        f"Reported issue:\n{case.reported_issue}\n\n"
        f"Logs attached by the reporter:\n{case.logs or 'None.'}"
    )


def build_graph(
    planner,
    write_report: Callable[[list[AnyMessage]], SupportAnalysis],
    tools,
    max_steps: int,
    missing_actions: Callable[[], list[str]] = lambda: [],
):
    def agent(state: InvestigationState) -> dict:
        response = planner.invoke([SystemMessage(SYSTEM_PROMPT), *state["messages"]])
        rounds = state["tool_rounds"] + (1 if response.tool_calls else 0)
        return {"messages": [response], "tool_rounds": rounds}

    def route(state: InvestigationState) -> str:
        last = state["messages"][-1]
        if state["tool_rounds"] > max_steps:
            return "finalize"
        if isinstance(last, AIMessage) and last.tool_calls:
            return "tools"
        if not state["reminded"] and missing_actions():
            return "remind"
        return "finalize"

    def remind(state: InvestigationState) -> dict:
        names = ", ".join(missing_actions())
        return {"messages": [HumanMessage(f"Before finishing you must call: {names}.")], "reminded": True}

    def finalize(state: InvestigationState) -> dict:
        history = list(state["messages"])
        if isinstance(history[-1], AIMessage) and history[-1].tool_calls:
            history = history[:-1]  # the app did not run the calls after the step limit
        analysis = write_report([SystemMessage(SYSTEM_PROMPT), *history, HumanMessage(FINALIZE_PROMPT)])
        return {"analysis": analysis}

    graph = StateGraph(InvestigationState)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode(tools, handle_tool_errors=True))
    graph.add_node("remind", remind)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route, {"tools": "tools", "remind": "remind", "finalize": "finalize"})
    graph.add_edge("tools", "agent")
    graph.add_edge("remind", "agent")
    graph.add_edge("finalize", END)
    return graph.compile()


def ollama_models(model: str, base_url: str, tools):
    from langchain_ollama import ChatOllama

    chat = ChatOllama(model=model, base_url=base_url, temperature=0, reasoning=False, num_ctx=16384)
    planner = chat.bind_tools(tools)
    writer = chat.with_structured_output(SupportAnalysis)
    return planner, writer.invoke


def run_investigation(
    box: InvestigationToolbox,
    model: str,
    max_steps: int,
    planner=None,
    write_report: Callable[[list[AnyMessage]], SupportAnalysis] | None = None,
    base_url: str = "http://127.0.0.1:11434",
    callbacks: list | None = None,
) -> AgentRunResult:
    tools = build_tools(box)
    if planner is None or write_report is None:
        planner, write_report = ollama_models(model, base_url, tools)
    graph = build_graph(planner, write_report, tools, max_steps, box.missing_required_actions)
    final = graph.invoke(
        {"messages": [HumanMessage(describe_case(box.case))], "tool_rounds": 0, "reminded": False, "analysis": None},
        {"recursion_limit": 2 * max_steps + 10, "callbacks": callbacks or []},
    )
    return AgentRunResult(
        case_id=box.case.id,
        model=model,
        analysis=final["analysis"],
        steps=box.steps,
        proposals=box.proposals,
        tool_rounds=min(final["tool_rounds"], max_steps),
        stopped_by_limit=final["tool_rounds"] > max_steps,
    )
