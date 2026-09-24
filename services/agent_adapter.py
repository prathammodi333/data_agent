"""Turn the existing LangGraph states into a stable browser response."""

import json
import logging
from langchain_core.messages import HumanMessage, ToolMessage
from utils.request_context import request_id as log_request_id

logger = logging.getLogger(__name__)

def invoke_agent(message: str, request_id: str = "local", source_artifact_id: str | None = None, source_mode: str = "auto") -> dict:
    token = log_request_id.set(request_id)
    try:
        return _invoke_agent(message, source_artifact_id, source_mode)
    finally:
        log_request_id.reset(token)


def _invoke_agent(message: str, source_artifact_id: str | None, source_mode: str = "auto") -> dict:
    # Keep import lazy so liveness does not initialize model clients or DB code.
    from agents.data_agent import data_agent
    result = data_agent.invoke(
        {"messages": [HumanMessage(content=message)], "route_response": "",
         "source_artifact_id": source_artifact_id, "source_mode": source_mode, "branch_result": None},
        config={"recursion_limit": 10},
    )
    route = result.get("route_response")
    branch = result.get("branch_result") or {}
    if route == "sql":
        if branch.get("is_safe", "No").lower() != "yes":
            reason = branch.get("comments") or "The generated SQL could not be validated."
            logger.warning("request=%s sql_blocked reason=%s", log_request_id.get(), reason)
            return {"status": "blocked", "route": "sql",
                    "answer": "I couldn't generate a permitted SQL query for this question.",
                    "table": None, "artifact": None,
                    "details": {"sql": None, "sql_safe": False, "operation": None},
                    "error": {"code": "UNSAFE_QUERY", "message": reason}}
        return {"status": "completed", "route": "sql",
                "answer": str(branch.get("final_answer", "")),
                "table": branch.get("sql_query_execution_result"), "artifact": None,
                "details": {"sql": branch.get("generated_sql_query"), "sql_safe": True, "operation": None},
                "error": None}
    if route == "etl":
        observations = []
        for item in branch.get("messages", []):
            if isinstance(item, ToolMessage):
                try:
                    observations.append(json.loads(item.content))
                except (ValueError, TypeError):
                    continue
        if observations:
            operation = observations[-1]
            return {"status": "completed", "route": "etl",
                    "answer": operation["answer"], "table": operation["table"],
                    "artifact": operation["artifact"],
                    "details": {"sql": operation.get("sql"), "sql_safe": True if operation.get("sql") else None,
                                "source_artifact_id": operation.get("source_artifact_id"),
                                "source_name": operation.get("source_name"),
                                "operation": operation["operation"]},
                    "error": None}
        return {"status": "unsupported", "route": "etl",
                "answer": "Provide a demo database query, a public JSON/CSV URL, or select an exported file to transform.",
                "table": None, "artifact": None,
                "details": {"sql": None, "sql_safe": None, "operation": None},
                "error": {"code": "UNSUPPORTED", "message": "Try one of the example prompts."}}
    raise ValueError("Unexpected graph route")
