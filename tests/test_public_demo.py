import json
import logging
import os
import csv
import sqlite3
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENAI_API_KEY", "test-key")

import pytest
from fastapi.testclient import TestClient

from api import app
from utils import etl_tools
from utils.database import UnsafeQuery, validate_select


@pytest.mark.parametrize("query", [
    "DELETE FROM public.rides",
    "SELECT * FROM public.rides; DROP TABLE public.rides",
    "SELECT * FROM pg_catalog.pg_user",
    "SELECT pg_sleep(30)",
    "SELECT * INTO new_table FROM public.rides",
    "SELECT * FROM public.users",
    "SELECT u.* FROM public.users AS u",
    "SELECT user_id FROM private.users",
    "SELECT user_id FROM otherdb.public.users",
    "WITH users AS (SELECT user_id FROM public.users) SELECT user_id FROM private.users",
    "SELECT public.lower('x') FROM public.users",
])
def test_sql_boundary_rejects_non_demo_or_unsafe_queries(query):
    with pytest.raises(UnsafeQuery):
        validate_select(query)


def test_sql_boundary_caps_select():
    checked = validate_select("SELECT payment_method, COUNT(*) FROM public.payments GROUP BY payment_method")
    assert checked.endswith("LIMIT 101")
    assert "public.payments" in checked


def test_sql_boundary_allows_count_on_users():
    checked = validate_select("SELECT COUNT(*) AS row_count FROM public.users")
    assert "COUNT(*) AS row_count FROM public.users" in checked


def test_sql_boundary_allows_average_fare_by_status():
    checked = validate_select(
        "SELECT status, ROUND(AVG(fare), 2) AS average_fare "
        "FROM public.rides GROUP BY status"
    )
    assert "public.rides GROUP BY status" in checked


def test_payment_method_aliases_generate_safe_stored_values():
    from agents.sql_analyst import known_read_query
    from utils.payment_intents import payment_intent

    expected = {
        "filter payments by payment method of credit card": ("'credit_card'",),
        "filter payments by debit card": ("'debit_card'",),
        "filter payments by card": ("'credit_card'", "'debit_card'"),
        "show me payments made with a debit card": ("'debit_card'",),
        "how many credit card payments?": ("'credit_card'",),
    }
    for question, values in expected.items():
        query = known_read_query(question)
        validate_select(query)
        assert "FROM public.payments WHERE payment_method IN" in query
        assert all(value in query for value in values)
        assert query.count("'credit_card'") == ("'credit_card'" in values)
        assert query.count("'debit_card'") == ("'debit_card'" in values)
    assert payment_intent("filter payments by card; DROP TABLE payments") is None


@pytest.mark.parametrize("question,expected_methods,is_count", [
    ("filter payments by payment method of credit card", {"credit_card"}, False),
    ("filter payments by debit card", {"debit_card"}, False),
    ("filter payments by card", {"credit_card", "debit_card"}, False),
    ("how many credit card payments?", {"credit_card"}, True),
])
def test_payment_aliases_query_actual_sample_values(monkeypatch, question, expected_methods, is_count):
    import agents.data_agent as router_module
    import agents.sql_analyst as sql_module
    from utils.database import MAX_ROWS

    with (Path(__file__).resolve().parents[1] / "data" / "payments.csv").open(newline="") as file:
        samples = list(csv.DictReader(file))[:1000]
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("ATTACH DATABASE ':memory:' AS public")
    conn.execute("CREATE TABLE public.payments (payment_id INTEGER, ride_id INTEGER, amount REAL, "
                 "payment_method TEXT, payment_status TEXT)")
    conn.executemany("INSERT INTO public.payments VALUES (?, ?, ?, ?, ?)", [
        (int(row["payment_id"]), int(row["ride_id"]), float(row["amount"]),
         row["payment_method"], row["payment_status"]) for row in samples
    ])

    class UnusedRouter:
        def invoke(self, prompt):
            pytest.fail("Known payment filter must bypass router model")

    class Judge:
        def with_structured_output(self, schema): return self
        def invoke(self, prompt): return SimpleNamespace(answer="Yes", comments="Read-only select")

    class DemoDatabase:
        def schema_details(self, name):
            pytest.fail("Known payment filter must skip generated SQL prompt")
        def execute_sql(self, query):
            validated = validate_select(query)
            cursor = conn.execute(validated)
            rows = cursor.fetchmany(MAX_ROWS + 1)
            return {"columns": [column[0] for column in cursor.description],
                    "rows": [list(row) for row in rows[:MAX_ROWS]],
                    "truncated": len(rows) > MAX_ROWS}

    monkeypatch.setattr(router_module, "llm_router", UnusedRouter())
    monkeypatch.setattr(sql_module, "pick_llm", lambda level: Judge())
    monkeypatch.setattr(sql_module, "DatabaseUtil", DemoDatabase)
    try:
        response = TestClient(app).post("/api/chat", json={"message": question})
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "completed"
        if is_count:
            expected_count = sum(row["payment_method"] in expected_methods for row in samples)
            assert expected_count > 0
            assert payload["table"]["rows"] == [[expected_count]]
            assert str(expected_count) in payload["answer"]
        else:
            assert payload["table"]["rows"]
            assert {row[3] for row in payload["table"]["rows"]}.issubset(expected_methods)
            assert {row["payment_method"] for row in samples}.issuperset(expected_methods)
            assert "No payments" not in payload["answer"]
    finally:
        conn.close()


def test_known_read_queries_match_only_supported_questions():
    from agents.sql_analyst import known_read_query
    assert known_read_query("number of rows in users table") == "SELECT COUNT(*) AS row_count FROM public.users"
    assert known_read_query("How many rows are in the users table?") == "SELECT COUNT(*) AS row_count FROM public.users"
    assert "AVG(fare)" in known_read_query("What is the average fare by ride status?")
    assert known_read_query("how many rows in private table") is None
    assert known_read_query("number of rows in users table; DROP TABLE users") is None


def test_demo_metadata_recognizes_only_explicit_approved_requests():
    from utils.demo_metadata import metadata_request
    assert metadata_request("which tables are available in db now?") == ("tables", None)
    assert metadata_request("how many tables are there in db?") == ("table_count", None)
    assert metadata_request("how many tables are there?") == ("table_count", None)
    assert metadata_request("Show me the schema of each table.") == ("schema", None)
    assert metadata_request("Show the schema of the rides table.") == ("schema", "rides")
    assert metadata_request("describe users table") == ("schema", "users")
    assert metadata_request("show the schema of private table") is None
    assert metadata_request("list tables; DROP TABLE users") is None
    with pytest.raises(UnsafeQuery):
        validate_select("SELECT table_name FROM information_schema.tables")


def test_metadata_lookups_are_parameterized_read_only(monkeypatch):
    from utils import database

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def execute(self, query, params=None): calls.append((query, params))
        def fetchall(self): return [("rides", "ride_id", "integer")]

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def set_session(self, **kwargs): sessions.append(kwargs)
        def cursor(self): return Cursor()
        def rollback(self): rollbacks.append(True)
        def close(self): pass

    calls, sessions, rollbacks = [], [], []
    monkeypatch.setattr(database.psycopg2, "connect", lambda **kwargs: Connection())
    result = database.DatabaseUtil(db_config={"dbname": "demo"}).demo_metadata("schema", "rides")
    assert result["rows"] == [["rides", "ride_id", "integer"]]
    assert calls[-1] == (database.DESCRIBE_DEMO_TABLES_SQL, (["rides"],))
    assert sessions == [{"readonly": True, "autocommit": False}]
    assert rollbacks == [True]
    with pytest.raises(UnsafeQuery):
        database.DatabaseUtil(db_config={"dbname": "demo"}).demo_metadata("schema", "pg_user")
    assert len(sessions) == 1


@pytest.mark.parametrize("message,expected", [
    ("which tables are available in db now?", "Available demo tables: rides, users."),
    ("how many tables are there in db?", "There are 2 available demo tables in this database."),
    ("how many tables are there?", "There are 2 available demo tables in this database."),
    ("Show me the schema of each table.", "Schema for the available demo tables is shown below."),
    ("Show the schema of the rides table.", "Schema for public.rides is shown below."),
])
def test_metadata_api_answers_without_any_model_calls(monkeypatch, message, expected):
    import agents.data_agent as router_module
    import agents.sql_analyst as sql_module

    class UnusedRouter:
        def invoke(self, prompt):
            pytest.fail("Metadata must not call the router model")

    class DemoDatabase:
        def __init__(self): pass
        def schema_details(self, schema_name):
            pytest.fail("Metadata must skip model SQL prompt construction")
        def demo_metadata(self, kind, table):
            if kind == "tables":
                return {"columns": ["table_name"], "rows": [["rides"], ["users"]], "truncated": False}
            if kind == "table_count":
                return {"columns": ["table_count"], "rows": [[2]], "truncated": False}
            return {"columns": ["table_name", "column_name", "data_type"],
                    "rows": [["rides", "ride_id", "integer"]], "truncated": False}

    monkeypatch.setattr(router_module, "llm_router", UnusedRouter())
    monkeypatch.setattr(sql_module, "pick_llm", lambda level: pytest.fail("Metadata must not call a model"))
    monkeypatch.setattr(sql_module, "DatabaseUtil", DemoDatabase)
    response = TestClient(app).post("/api/chat", json={"message": message})
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["answer"] == expected
    assert response.json()["table"]["rows"]


def test_demo_export_read_only_and_excludes_sensitive_columns(monkeypatch):
    from utils import database
    calls, sessions = [], []

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, query, params=None): calls.append((query, params))
        def fetchall(self): return [(7, "Toronto", "ON", "rider", None, True)]

    class Connection:
        def cursor(self): return Cursor()
        def set_session(self, **kwargs): sessions.append(kwargs)
        def rollback(self): pass
        def close(self): pass

    monkeypatch.setattr(database.psycopg2, "connect", lambda **kwargs: Connection())
    result = database.DatabaseUtil(db_config={"dbname": "demo"}).export_demo_table("users")
    assert result["columns"] == ["user_id", "city", "province", "user_type", "signup_date", "is_active"]
    assert result["rows"] == [(7, "Toronto", "ON", "rider", None, True)]
    assert sessions == [{"readonly": True, "autocommit": False}]
    query, params = calls[-1]
    assert "FROM public.users ORDER BY user_id LIMIT %s" in query
    assert params == (501,)
    assert "email" not in query and "phone" not in query and "first_name" not in query
    with pytest.raises(UnsafeQuery):
        database.DatabaseUtil(db_config={"dbname": "demo"}).export_demo_table("orders")


def test_users_database_export_then_transform_csv_via_api(tmp_path, monkeypatch):
    import agents.data_agent as router_module
    monkeypatch.setattr(etl_tools, "ARTIFACT_ROOT", tmp_path)

    class UnusedRouter:
        def invoke(self, prompt):
            pytest.fail("Approved database ETL must skip the router model")

    class DemoDatabase:
        def export_demo_table(self, table):
            assert table == "users"
            return {"columns": ["user_id", "city", "province", "user_type", "signup_date", "is_active"],
                    "rows": [(1, "Hamilton", "ON", "rider", "2025-01-01", True),
                             (2, "Ottawa", "ON", "driver", "2025-02-01", False)], "truncated": False}

    monkeypatch.setattr(router_module, "llm_router", UnusedRouter())
    monkeypatch.setattr(etl_tools, "DatabaseUtil", DemoDatabase)
    monkeypatch.setattr(etl_tools.requests, "get", lambda *args, **kwargs: pytest.fail("Never use Pokémon for DB ETL"))
    client = TestClient(app)
    export = client.post("/api/chat", json={"message": "extract users table from the db"})
    assert export.status_code == 200
    payload = export.json()
    assert payload["route"] == "etl" and payload["status"] == "completed"
    artifact = payload["artifact"]
    assert artifact["name"] == "users_export.csv"
    assert payload["table"]["columns"] == ["user_id", "city", "province", "user_type", "signup_date", "is_active"]
    csv_response = client.get(artifact["download_url"])
    assert csv_response.status_code == 200
    assert "email" not in csv_response.text

    filtered = client.post("/api/chat", json={"message": "Filter active users from exported users CSV",
                                                  "source_artifact_id": artifact["id"]})
    assert filtered.status_code == 200
    assert filtered.json()["artifact"]["name"] == "active_users.csv"
    assert filtered.json()["table"]["rows"] == [[1, "Hamilton", "ON", "rider", "2025-01-01", True]]
    grouped = client.post("/api/chat", json={"message": "Count users by province from exported users CSV",
                                                 "source_artifact_id": artifact["id"]})
    assert grouped.status_code == 200
    assert grouped.json()["table"]["rows"] == [["ON", 2]]

    without_source = client.post("/api/chat", json={"message": "Filter active users from exported users CSV"})
    assert without_source.json()["status"] == "blocked"
    assert "Export the users table" in without_source.json()["error"]["message"]
    unsupported = client.post("/api/chat", json={"message": "extract orders table from the db"})
    assert unsupported.json()["status"] == "blocked"
    assert "approved tables" in unsupported.json()["error"]["message"]


def test_model_generated_sql_fence_is_removed_before_validation(monkeypatch):
    import agents.sql_analyst as module

    class Model:
        def invoke(self, prompt):
            return SimpleNamespace(content="```sql\nSELECT payment_method, COUNT(*) FROM public.payments GROUP BY payment_method\n```")

    monkeypatch.setattr(module, "pick_llm", lambda level: Model())
    state = SimpleNamespace(user_question="Summarize payments by method", prompt_query_context="test",
                            generated_sql_query="")
    module.generate_sql(state)
    assert state.generated_sql_query.startswith("SELECT payment_method")
    validate_select(state.generated_sql_query)


def test_users_count_runs_through_sql_graph(monkeypatch):
    import agents.sql_analyst as module

    class Model:
        def invoke(self, prompt):
            if "Review this PostgreSQL SELECT" in prompt:
                return SimpleNamespace(answer="No", comments="Mistakenly flagged aggregate as unsafe")
            if "Database Schema Details:" in prompt:
                content = "SELECT COUNT(*) AS row_count FROM public.users"
            else:
                content = "There are 12 rows in the users table."
            return SimpleNamespace(content=content)

        def with_structured_output(self, schema):
            return self

    class DemoDatabase:
        def schema_details(self, schema_name):
            return "Table: public.users\n  user_id: integer"

        def execute_sql(self, query):
            validate_select(query)
            return {"columns": ["row_count"], "rows": [[12]], "truncated": False}

    monkeypatch.setattr(module, "pick_llm", lambda level: Model())
    monkeypatch.setattr(module, "DatabaseUtil", DemoDatabase)
    result = module.sql_analyst.invoke({
        "messages": [], "user_question": "number of rows in users table",
        "curated_ques": "", "prompt_query_context": "", "generated_sql_query": "",
        "is_safe": "No", "comments": "", "sql_query_execution_result": "",
        "final_answer": "",
    })
    assert result["is_safe"] == "Yes"
    assert result["sql_query_execution_result"]["rows"] == [[12]]
    assert result["final_answer"] == "There are 12 rows in the users table."


def test_average_fare_api_works_and_logs_advisory_judge(monkeypatch, caplog):
    import agents.data_agent as router_module
    import agents.sql_analyst as sql_module

    class Router:
        def invoke(self, message):
            return SimpleNamespace(model_dump=lambda: {"answer": "sql"})

    class Model:
        def invoke(self, prompt):
            if "Review this PostgreSQL SELECT" in prompt:
                return SimpleNamespace(answer="No", comments="False positive: aggregate")
            if "Database Schema Details:" in prompt:
                return SimpleNamespace(content="```sql\nSELECT status, AVG(fare) AS average_fare FROM public.rides GROUP BY status\n```")
            return SimpleNamespace(content="Average fares are 12.5 for completed and 8.0 for cancelled rides.")

        def with_structured_output(self, schema):
            return self

    class DemoDatabase:
        def schema_details(self, schema_name):
            return "Table: public.rides\n  status: character varying\n  fare: numeric"

        def execute_sql(self, query):
            validate_select(query)
            return {"columns": ["status", "average_fare"],
                    "rows": [["completed", 12.5], ["cancelled", 8.0]], "truncated": False}

    monkeypatch.setattr(router_module, "llm_router", Router())
    monkeypatch.setattr(sql_module, "pick_llm", lambda level: Model())
    monkeypatch.setattr(sql_module, "DatabaseUtil", DemoDatabase)
    with caplog.at_level(logging.INFO):
        response = TestClient(app).post("/api/chat", json={"message": "What is the average fare by ride status?"})
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["answer"] == "Average fare by ride status: completed: 12.50; cancelled: 8.00."
    assert response.json()["table"]["rows"][0] == ["completed", 12.5]
    request_id = response.json()["request_id"]
    assert any(request_id in record.message and "generated_sql=" in record.message for record in caplog.records)
    assert any(request_id in record.message and "llm_judge=No reason=False positive" in record.message for record in caplog.records)
    assert any(request_id in record.message and "sql_executed rows=2" in record.message for record in caplog.records)


def test_unsafe_sql_reports_validator_reason(monkeypatch):
    import agents.data_agent as router_module
    import agents.sql_analyst as sql_module

    class Router:
        def invoke(self, message):
            return SimpleNamespace(model_dump=lambda: {"answer": "sql"})

    class Model:
        def invoke(self, prompt):
            return SimpleNamespace(content="DELETE FROM public.rides")

    class DemoDatabase:
        def query_context(self):
            return {"schema": "Table: public.rides\n  ride_id: integer", "columns": {"rides": {"ride_id": "integer"}}, "values": {}}

    monkeypatch.setattr(router_module, "llm_router", Router())
    monkeypatch.setattr(sql_module, "pick_llm", lambda level: Model())
    monkeypatch.setattr(sql_module, "DatabaseUtil", DemoDatabase)
    response = TestClient(app).post("/api/chat", json={"message": "Delete all rides"})
    assert response.status_code == 200
    assert response.json()["status"] == "blocked"
    assert response.json()["error"]["message"] == "Only one SELECT statement is allowed"


def test_flat_file_etl_and_artifact_isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(etl_tools, "ARTIFACT_ROOT", tmp_path)
    first = etl_tools.ETLTools().filter_ratings(4.5, "csv")
    second = etl_tools.ETLTools().filter_ratings(4.5, "json")
    assert first["artifact"]["id"] != second["artifact"]["id"]
    assert first["table"]["columns"] == ["ride_id", "rating"]
    file, info = etl_tools.artifact_info(first["artifact"]["id"])
    assert file.parent == tmp_path
    assert file.suffix == ".csv"
    assert info["name"] == "filtered_ratings.csv"
    with pytest.raises(etl_tools.ETLRejected):
        etl_tools.artifact_info("../../.env")
    with pytest.raises(etl_tools.ETLRejected):
        etl_tools.ETLTools().filter_ratings(99)


def test_approved_extraction_only_and_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(etl_tools, "ARTIFACT_ROOT", tmp_path)
    class Response:
        status_code = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, chunk_size):
            yield json.dumps({"results": [{"name": "bulbasaur", "url": "https://pokeapi.co/api/v2/pokemon/1/"}]}).encode()
    seen = {}
    def fake_get(url, **kwargs):
        seen.update(url=url, **kwargs)
        return Response()
    monkeypatch.setattr(etl_tools.requests, "get", fake_get)
    result = etl_tools.ETLTools().extract_pokemon("csv")
    assert seen["url"] == etl_tools.POKEMON_URL
    assert seen["allow_redirects"] is False
    assert result["artifact"]["format"] == "csv"


def test_api_validation_and_contract(monkeypatch):
    from services import agent_adapter
    monkeypatch.setattr(agent_adapter, "invoke_agent", lambda message, request_id, source_artifact_id, source_mode: {
        "status": "completed", "route": "sql", "answer": "Two methods.",
        "table": {"columns": ["method"], "rows": [["cash"]], "truncated": False},
        "artifact": None, "details": {"sql": "SELECT ...", "sql_safe": True, "operation": None},
        "error": None,
    })
    client = TestClient(app)
    assert client.get("/api/health").json() == {"status": "ok", "revision": "artifact-source-priority-7"}
    assert client.post("/api/chat", json={"message": "  "}).status_code == 422
    assert client.post("/api/chat", json={"message": "Fetch https://example.org"}).status_code == 200
    response = client.post("/api/chat", json={"message": "How many payment methods?"})
    assert response.status_code == 200
    assert response.json()["route"] == "sql"
    assert response.json()["table"]["rows"] == [["cash"]]
    assert client.get("/api/artifacts/not-an-id").status_code == 404


def test_adapter_normalizes_nested_sql_and_etl(monkeypatch):
    import agents.data_agent as module
    from services.agent_adapter import invoke_agent
    from langchain_core.messages import ToolMessage
    class Graph:
        result = None
        def invoke(self, *args, **kwargs): return self.result
    graph = Graph()
    monkeypatch.setattr(module, "data_agent", graph)
    graph.result = {"route_response": "sql", "branch_result": {
        "is_safe": "Yes", "final_answer": "42", "generated_sql_query": "SELECT 42",
        "sql_query_execution_result": {"columns": ["answer"], "rows": [[42]], "truncated": False}}}
    assert invoke_agent("test")["table"]["rows"] == [[42]]
    payload = {"answer": "Saved", "table": None, "artifact": {"id": "id"}, "operation": "filter_ratings"}
    graph.result = {"route_response": "etl", "branch_result": {"messages": [
        ToolMessage(content=json.dumps(payload), tool_call_id="test")
    ]}}
    assert invoke_agent("test")["artifact"]["id"] == "id"


def test_real_graph_state_shape_with_mocked_router(monkeypatch):
    import agents.data_agent as module
    from langchain_core.messages import HumanMessage
    class Router:
        def invoke(self, message):
            return SimpleNamespace(model_dump=lambda: {"answer": "sql"})
    class Analyst:
        def invoke(self, state):
            return {"is_safe": "Yes", "final_answer": "done"}
    monkeypatch.setattr(module, "llm_router", Router())
    monkeypatch.setattr(module, "sql_analyst", Analyst())
    result = module.data_agent.invoke({"messages": [HumanMessage(content="test")],
                                       "route_response": "", "branch_result": None})
    assert result["route_response"] == "sql"
    assert result["branch_result"]["final_answer"] == "done"
    assert len(result["messages"]) == 1


def test_etl_graph_calls_one_flat_file_tool(tmp_path, monkeypatch):
    import agents.etl_analyst as module
    from langchain_core.messages import AIMessage, HumanMessage
    monkeypatch.setattr(etl_tools, "ARTIFACT_ROOT", tmp_path)
    class Model:
        def invoke(self, prompt):
            return AIMessage(content="", tool_calls=[{
                "name": "filter_ratings_tool", "args": {"min_rating": 4.5, "format": "csv"}, "id": "call-1"
            }])
    monkeypatch.setattr(module, "llm_bind", Model())
    result = module.etl_analyst.invoke({"messages": [HumanMessage(content="Filter ratings")]})
    assert len(result["messages"]) == 4
    operation = json.loads(result["messages"][2].content)
    assert operation["operation"] == "filter_ratings"
    assert etl_tools.artifact_info(operation["artifact"]["id"])[0].suffix == ".csv"
