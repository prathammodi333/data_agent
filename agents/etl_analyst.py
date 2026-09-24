"""LangGraph ETL branch: LLM chooses a constrained flat-file tool."""

import json
import re

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain.tools import tool
from langgraph.graph import StateGraph, START, END

from Models.schema import ETLAgentSchema
from utils.etl_tools import ETLTools, ETLRejected
from utils.llm_pick import pick_llm


@tool
def extract_pokemon_tool(format: str = "csv") -> str:
    """Extract the approved Pokémon API source into a CSV, JSON Lines or Parquet file."""
    return json.dumps(ETLTools().extract_pokemon(format))


@tool
def filter_ratings_tool(min_rating: float = 4.5, format: str = "csv") -> str:
    """Filter the bundled ratings flat file and export a flat file."""
    return json.dumps(ETLTools().filter_ratings(min_rating, format))


@tool
def summarize_payments_tool(format: str = "csv") -> str:
    """Summarize the bundled payments flat file by method and export a flat file."""
    return json.dumps(ETLTools().summarize_payments(format))


tools = [extract_pokemon_tool, filter_ratings_tool, summarize_payments_tool]
llm_bind = pick_llm("etl").bind_tools(tools)


def llm_node(state: ETLAgentSchema):
    if state.messages and isinstance(state.messages[-1], ToolMessage):
        state.messages = state.messages + [AIMessage(content=json.loads(state.messages[-1].content)["answer"])]
        return state
    prompt = """You are a demo ETL assistant. Select exactly one available tool for the
    user's request. You can only use the named demo CSV sources or the approved
    Pokémon source. Load results only to flat files; never write to a database.
    Do not invent success. If no tool supports the request, say it is unsupported.
    Here is the request and tool history: """ + str(state.messages)
    state.messages = state.messages + [llm_bind.invoke(prompt)]
    return state


def tool_node(state: ETLAgentSchema):
    calls = state.messages[-1].tool_calls
    if len(calls) != 1:
        raise ValueError("Exactly one ETL operation is supported per request")
    call = calls[0]
    selected = {item.name: item for item in tools}.get(call["name"])
    if selected is None:
        raise ValueError("Unsupported ETL operation")
    request = next((item.content for item in state.messages if isinstance(item, HumanMessage)), "")
    # A tool producing aggregates cannot fulfill a request for filtered records.
    if call["name"] == "summarize_payments_tool" and (
        not re.search(r"\b(?:summari[sz]e|summary|breakdown|group|count|total|number)\b|how many", request, re.I)
        or re.search(r"\b(?:filter|where|only)\b", request, re.I)
    ):
        raise ETLRejected("A payment summary cannot fulfill a filtered-row export. Specify payments and the filter to read from the demo database.")
    observation = selected.invoke(call["args"])
    state.messages = state.messages + [ToolMessage(content=observation, tool_call_id=call["id"])]
    return state


graph = StateGraph(ETLAgentSchema)
graph.add_node("llm_node", llm_node)
graph.add_node("tool_node", tool_node)
graph.add_edge(START, "llm_node")
graph.add_conditional_edges("llm_node", lambda state: "tool_node" if state.messages[-1].tool_calls else "end",
                            {"tool_node": "tool_node", "end": END})
graph.add_edge("tool_node", "llm_node")
etl_analyst = graph.compile()
