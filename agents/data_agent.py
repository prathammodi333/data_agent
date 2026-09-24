import os
import sys
import logging

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from agents import sql_analyst
from utils.llm_pick import pick_llm
from utils.etl_tools import ETLTools
from Models.schema import RouterSchema, DataAgentSchema
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import StateGraph, START, END
from langchain.tools import tool
from agents.etl_analyst import etl_analyst
from agents.sql_analyst import sql_analyst
from utils.request_context import request_id
from utils.demo_metadata import metadata_request
from utils.payment_intents import payment_intent
from utils.etl_tools import (ETLRejected, demo_etl_request, requests_database_etl,
                             artifact_reference_name, DB_EXPORT_RE, DEMO_TABLE_ALIASES)
from utils.safe_fetch import source_url
from utils.source_selection import select_source
import re
import json

logger = logging.getLogger(__name__)

llm = pick_llm("claude")

llm_router = llm.with_structured_output(RouterSchema)


# ---------------------------- DATA AGENT GRAPH ---------------------------- #


def router_node(state:DataAgentSchema):

    message = state.messages[-1].content
    metadata = metadata_request(message)
    known_etl = demo_etl_request(message)
    kind, source = select_source(message, state.source_mode)
    state.source_kind = kind
    if kind in {"url", "artifact"}:
        route_response = "etl"
    elif metadata:
        route_response, source = "sql", "trusted_metadata"
    elif payment_intent(message):
        route_response, source = "sql", "trusted_payment_filter"
    elif (known_etl and known_etl[0] == "extract_demo_table") or requests_database_etl(message):
        route_response, source = "etl", "trusted_db_etl"
    elif kind == "database":
        route_response, source = "sql", "demo_database"
    else:
        route_response, source = llm_router.invoke(message).model_dump()["answer"], "model"

    state.route_response = route_response
    logger.info("request=%s route=%s source=%s source_kind=%s source_mode=%s artifact_id=%s", request_id.get(), route_response,
                source, kind, state.source_mode, state.source_artifact_id if kind == "artifact" else None)

    return state

def etl_node(state:DataAgentSchema):

    message = state.messages[-1].content
    known_etl = demo_etl_request(message)
    url = source_url(message)
    if state.source_kind == "url":
        result = ETLTools().extract_url(url, message)
        return etl_result(state, result)
    if state.source_kind == "artifact":
        if known_etl and known_etl[0] != "extract_demo_table" and not artifact_reference_name(message):
            result = ETLTools().transform_users_export(state.source_artifact_id, known_etl[0])
            return etl_result(state, result)
        result = ETLTools().transform_artifact(state.source_artifact_id, message)
        return etl_result(state, result)
    if known_etl and known_etl[0] == "extract_demo_table":
        operation, table = known_etl
        if operation == "extract_demo_table":
            result = ETLTools().extract_demo_table(table)
        else:
            result = ETLTools().transform_users_export(state.source_artifact_id, operation)
        logger.info("request=%s etl_operation=%s table=%s output_rows=%d", request_id.get(),
                    operation, table, len(result["table"]["rows"]))
        state.branch_result = {"messages": [ToolMessage(content=json.dumps(result), tool_call_id="trusted-db-etl")]}
        return state
    if requests_database_etl(message):
        logger.info("request=%s etl_source=demo_database previous_artifact_ignored=%s",
                    request_id.get(), bool(state.source_artifact_id))
        explicit = DB_EXPORT_RE.fullmatch(message.strip().rstrip("?.!"))
        if explicit and explicit.group("table").lower() not in DEMO_TABLE_ALIASES:
            raise ETLRejected("Only approved tables in the demo database can be exported.")
        response = sql_analyst.invoke(sql_input(message, export_mode=True))
        if response.get("is_safe") != "Yes":
            raise ETLRejected(response.get("comments") or "Could not validate the export query.")
        result = ETLTools().export_query_result(response["sql_query_execution_result"],
                                               response["generated_sql_query"], message)
        return etl_result(state, result)
    response = etl_analyst.invoke(
             {"messages":[HumanMessage(content=f"""
            {message}
    """)]}
        ) 
    state.branch_result = response

    return state


def etl_result(state, result):
    logger.info("request=%s etl_operation=%s output_rows=%s source=%s", request_id.get(),
                result["operation"], result["artifact"].get("row_count"), result["artifact"].get("source"))
    state.branch_result = {"messages": [ToolMessage(content=json.dumps(result), tool_call_id="dataset-etl")]}
    return state


def sql_input(message, export_mode=False):
    return {"messages": [], "user_question": message, "curated_ques": "", "prompt_query_context": "",
            "generated_sql_query": "", "is_safe": "No", "comments": "", "sql_query_execution_result": "",
            "final_answer": "", "export_mode": export_mode}

def sql_node(state:DataAgentSchema):

    message = state.messages[-1].content

    input_schema = sql_input(message)

    response = sql_analyst.invoke(input_schema)

    state.branch_result = response

    return state




data_agent_graph = StateGraph(DataAgentSchema)

data_agent_graph.add_node("router_node", router_node)
data_agent_graph.add_node("etl_node", etl_node)
data_agent_graph.add_node("sql_node", sql_node)

data_agent_graph.add_edge(START, "router_node")

def route_edge(state: DataAgentSchema) -> str:
    if state.route_response == "sql":
        return "sql_node"
    elif state.route_response == "etl":
        return "etl_node"
    else:
        raise ValueError(f"Invalid route response: {state.route_response}")


data_agent_graph.add_conditional_edges("router_node", route_edge,
                                      {
                                          "sql_node": "sql_node",
                                          "etl_node": "etl_node"
                                      })

data_agent = data_agent_graph.compile()

if __name__ == "__main__":

    # response = data_agent.invoke(
    #     {"messages":[HumanMessage(content="I want to extract the data from the API endpoint 'https://pokeapi.co/api/v2/pokemon' and save it to data/extract folder in the csv folder")],
    #      "route_response": ""}
    # )

    response = data_agent.invoke(
            {"messages":[HumanMessage(content="What are the different types of Payment Methods we have in our database?")],
             "route_response": ""}
        )
    print(response)
