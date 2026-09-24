"""API regressions execute generated SQL and transformation plans on real sample rows."""

import csv
import io
import json
import socket
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api import app
from utils import etl_tools, safe_fetch, transformations
from utils.data_sources import parse_data
from utils.database import validate_select
from utils.errors import ETLRejected
from utils.transformations import TransformPlan, apply_plan
from utils.value_matching import normalize_filter_values


PAYMENT_CONTEXT = {
    "schema": "public.payments(payment_id integer, ride_id integer, amount numeric, payment_method text, payment_status text)",
    "columns": {"payments": {"payment_id": "integer", "ride_id": "integer", "amount": "numeric",
                             "payment_method": "text", "payment_status": "text"}},
    "values": {"payments.payment_method": ["credit_card", "debit_card", "paypal"],
               "payments.payment_status": ["completed", "failed"]},
}


@pytest.fixture(autouse=True)
def test_rate_limit(monkeypatch):
    monkeypatch.setenv("REQUESTS_PER_HOUR", "1000")


@pytest.mark.parametrize("phrase,literal,expected", [
    ("PAYPAL", "PAYPAL", {"paypal"}),
    ("paypal", "paypal", {"paypal"}),
    ("credit card", "credit card", {"credit_card"}),
    ("debit card", "debit card", {"debit_card"}),
    ("card", "card", {"credit_card", "debit_card"}),
    ("google pay", "google pay", {"google_pay"}),
    ("apple pay", "apple pay", {"apple_pay"}),
])
@pytest.mark.parametrize("subject,with_previous_summary", [("payments", False), ("payment", False), ("payment", True)])
def test_filter_export_api_executes_sql_and_downloads_csv(tmp_path, monkeypatch, caplog, phrase, literal, expected, subject, with_previous_summary):
    import agents.sql_analyst as sql_module
    import agents.data_agent as router_module
    monkeypatch.setattr(etl_tools, "ARTIFACT_ROOT", tmp_path)
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("ATTACH DATABASE ':memory:' AS public")
    conn.execute("CREATE TABLE public.payments(payment_id INTEGER,ride_id INTEGER,amount REAL,payment_method TEXT,payment_status TEXT)")
    with (Path(__file__).resolve().parents[1] / "data/payments.csv").open() as file:
        rows = list(csv.DictReader(file))[:1000]
    conn.executemany("INSERT INTO public.payments VALUES(?,?,?,?,?)", [
        (int(row["payment_id"]), int(row["ride_id"]), float(row["amount"]), row["payment_method"], row["payment_status"]) for row in rows])
    seen = []

    class Model:
        def with_structured_output(self, schema): return self
        def invoke(self, prompt):
            if "Review this PostgreSQL SELECT" in prompt:
                return SimpleNamespace(answer="Yes", comments="Read-only demo filter")
            assert "credit_card" in prompt and "Stored value examples" in prompt
            assert "Apply every filter before the cap" in prompt
            return SimpleNamespace(content="SELECT payment_id, ride_id, amount, payment_method, payment_status "
                                   f"FROM public.payments WHERE payment_method = '{literal}' ORDER BY payment_id")

    class Database:
        def query_context(self): return PAYMENT_CONTEXT
        def execute_sql(self, query, max_rows):
            assert max_rows == 500
            seen.append(query)
            cursor = conn.execute(validate_select(query, max_rows))
            records = cursor.fetchmany(max_rows + 1)
            return {"columns": [col[0] for col in cursor.description], "rows": [list(row) for row in records[:max_rows]],
                    "truncated": len(records) > max_rows}

    monkeypatch.setattr(sql_module, "DatabaseUtil", Database)
    monkeypatch.setattr(sql_module, "pick_llm", lambda level: Model())
    monkeypatch.setattr(router_module, "llm_router", SimpleNamespace(invoke=lambda _: pytest.fail("No router inference needed")))
    monkeypatch.setattr(router_module, "etl_analyst", SimpleNamespace(invoke=lambda _: pytest.fail("Row export must not use legacy summary tool")))
    monkeypatch.setattr(etl_tools.ETLTools, "transform_artifact", lambda *a, **k: pytest.fail("Named table requests must not transform the previous summary"))
    monkeypatch.setattr(etl_tools.requests, "get", lambda *a, **k: pytest.fail("No unrelated API"))
    try:
        client = TestClient(app)
        request = {"message": f"Filter {subject} by payment method of {phrase} and extract to csv"}
        if with_previous_summary:
            summary = etl_tools._save(pd.DataFrame({"payment_method": ["paypal", "google_pay"],
                "payment_count": [3207,3295], "total_amount": [204265.38,214610.64]}),
                "payment_summary", "csv", "summarize_payments", "demo payments CSV")
            request["source_artifact_id"] = summary["artifact"]["id"]
        with caplog.at_level("INFO"):
            response = client.post("/api/chat", json=request)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "completed", result
        assert result["route"] == "etl" and result["details"]["sql_safe"] is True
        download = client.get(result["artifact"]["download_url"])
        records = list(csv.DictReader(io.StringIO(download.text)))
        assert "payment_id" in records[0] and "payment_count" not in records[0]
        matching = [row for row in rows if row["payment_method"] in expected]
        assert len(records) == min(500, len(matching)) > 0
        assert {row["payment_method"] for row in records} == expected
        assert [row["payment_id"] for row in records] == [row["payment_id"] for row in matching[:500]]
        assert "value_normalization=applied" in caplog.text and "llm_judge=Yes" in caplog.text
        if with_previous_summary:
            assert "previous_artifact_ignored=True" in caplog.text
    finally:
        conn.close()


def test_summary_tool_cannot_satisfy_filter_request(tmp_path, monkeypatch):
    import agents.etl_analyst as module
    from langchain_core.messages import AIMessage, HumanMessage
    monkeypatch.setattr(etl_tools, "ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(module, "llm_bind", SimpleNamespace(invoke=lambda _: AIMessage(content="", tool_calls=[{
        "name":"summarize_payments_tool", "args":{}, "id":"bad-call"}])))
    with pytest.raises(ETLRejected, match="summary cannot fulfill"):
        module.etl_analyst.invoke({"messages":[HumanMessage(content="filter payment by payment method of google pay and extract to csv")]})
    assert not list(tmp_path.glob("*.meta"))


def test_export_aggregate_guard_preserves_requested_row_grain(monkeypatch):
    import agents.sql_analyst as module
    from agents.data_agent import sql_input
    from Models.schema import AgentSchema
    class Judge:
        def with_structured_output(self, schema): return self
        def invoke(self, prompt): return SimpleNamespace(answer="Yes", comments="Read only")
    monkeypatch.setattr(module,"pick_llm",lambda _:Judge())
    state=AgentSchema(**sql_input("filter payment by payment method of paypal and extract to csv", export_mode=True))
    state.generated_sql_query="SELECT COUNT(*) AS payment_count FROM public.payments WHERE payment_method = 'paypal'"
    module.is_safe_sql(state)
    assert state.is_safe == "No" and "individual filtered rows" in state.comments
    state.user_question="Count payments where payment method is paypal and export to CSV"
    module.is_safe_sql(state)
    assert state.is_safe == "Yes"


@pytest.mark.parametrize("message,table", [("filter payment by amount over 20 and extract csv", "payments"),
    ("export ride where fare is over 20", "rides"), ("export vehicle to CSV", "vehicles"),
    ("filter rating above 4 and export", "ratings"), ("export user to CSV", "users")])
def test_singular_database_sources(message, table):
    assert table in etl_tools.referenced_demo_tables(message)
    assert etl_tools.requests_database_etl(message)


def test_filters_on_multiple_columns_and_unseen_values_execute():
    context = {"columns": {"users": {"city": "text", "email": "text", "is_active": "boolean", "user_id": "integer"}},
               "values": {"users.city": ["Toronto"]}}
    raw = "SELECT user_id FROM public.users WHERE city = 'NEW YORK' AND email = 'a@example.com' AND is_active = TRUE AND user_id > 1"
    query = normalize_filter_values(raw, context)
    conn = sqlite3.connect(":memory:")
    conn.execute("ATTACH DATABASE ':memory:' AS public")
    conn.execute("CREATE TABLE public.users(user_id INTEGER,city TEXT,email TEXT,is_active BOOLEAN)")
    conn.executemany("INSERT INTO public.users VALUES(?,?,?,?)", [(1,"new_york","a@example.com",1),
        (2,"new_york","a@example.com",1),(3,"new_york","a@example.com",0),(4,"Toronto","a@example.com",1)])
    assert conn.execute(validate_select(query)).fetchall() == [(2,)]
    conn.close()


def test_url_extract_transform_followup_via_api(tmp_path, monkeypatch):
    monkeypatch.setattr(etl_tools, "ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(safe_fetch, "fetch_public_data", lambda url: (
        json.dumps({"data": [{"id":1,"method":"credit_card","amount":10},
                              {"id":2,"method":"paypal","amount":25},
                              {"id":3,"method":"PAYPAL","amount":30}]}).encode(), "application/json", "public URL example.org"))
    plans = [TransformPlan.model_validate({"steps":[{"operation":"filter","filters":[
        {"column":"method","operator":"eq","values":["PayPal"]}]}]}),
        TransformPlan.model_validate({"format":"json","steps":[{"operation":"group","columns":["method"],
            "aggregates":[{"column":"amount","function":"sum","output":"total"}]}]})]
    monkeypatch.setattr(transformations, "plan_transform", lambda message, df: plans.pop(0))
    client = TestClient(app)
    first = client.post("/api/chat", json={"message":"Extract https://example.org/data.json, filter method PayPal, and export CSV"})
    assert first.status_code == 200, first.text
    payload = first.json()
    assert payload["status"] == "completed", payload
    assert payload["table"]["rows"] == [[2,"paypal",25],[3,"PAYPAL",30]]
    download = client.get(payload["artifact"]["download_url"])
    assert "credit_card" not in download.text
    followup = client.post("/api/chat", json={"message":"Group this file by method and sum amount, export as JSON",
                                               "source_artifact_id":payload["artifact"]["id"]})
    assert followup.status_code == 200, followup.text
    assert followup.json()["status"] == "completed", followup.json()
    assert followup.json()["artifact"]["format"] == "json"
    assert followup.json()["table"]["rows"] == [["PAYPAL",30],["paypal",25]]
    missing = client.post("/api/chat", json={"message":"Filter this file by amount above 20"})
    assert missing.json()["status"] == "blocked"


@pytest.mark.parametrize("body,kind,columns,rows", [
    (b'[{"id":1,"address":{"city":"Hamilton"}}]', "application/json", ["id","address.city"], [[1,"Hamilton"]]),
    (b'{"id":1}\n{"id":2}', "application/x-ndjson", ["id"], [[1],[2]]),
    (b'id,amount\n1,23\n2,45', "text/csv", ["id","amount"], [[1,23],[2,45]]),
    (b'id\tamount\n1\t23\n2\t45', "text/tab-separated-values", ["id","amount"], [[1,23],[2,45]]),
])
def test_data_formats(body, kind, columns, rows):
    frame = parse_data(body, kind)
    assert list(frame.columns) == columns
    assert frame.values.tolist() == rows


def test_ambiguous_json_requires_explicit_path():
    body = b'{"data":{"items":[{"id":1}],"errors":[{"code":2}]}}'
    with pytest.raises(ETLRejected, match="multiple record arrays"):
        parse_data(body, "application/json")
    assert parse_data(body, "application/json", "data.items")["id"].tolist() == [1]


def test_transform_plan_executes_operations_and_rejects_code():
    data = pd.DataFrame({"payment_method":["credit_card","debit_card","paypal","credit_card"],
                         "amount":["12","20","5","12"], "region":[None,"ON","BC",None]})
    plan = TransformPlan.model_validate({"steps":[
        {"operation":"filter","filters":[{"column":"payment method","operator":"eq","values":["card"]}]},
        {"operation":"cast","columns":["amount"],"dtype":"number"},
        {"operation":"fill_null","columns":["region"],"value":"Unknown"},
        {"operation":"drop_duplicates"},
        {"operation":"group","columns":["region"],"aggregates":[{"column":"amount","function":"sum","output":"total"}]},
        {"operation":"rename","renames":[{"old":"total","new":"sum_amount"}]},
        {"operation":"sort","columns":["sum_amount"],"ascending":False},
        {"operation":"select","columns":["region","sum_amount"]},
        {"operation":"limit","count":1}]})
    assert apply_plan(data, plan).values.tolist() == [["ON",20]]
    with pytest.raises(ValueError):
        TransformPlan.model_validate({"steps":[{"operation":"python","code":"os.system('id')"}]})
    with pytest.raises(ETLRejected, match="Unknown"):
        apply_plan(data, TransformPlan.model_validate({"steps":[{"operation":"select","columns":["absent"]}]}))


def dns_addresses(*ips):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ips]


@pytest.mark.parametrize("ip", ["127.0.0.1","10.0.0.5","169.254.169.254","192.168.1.10","::1","::ffff:127.0.0.1","224.0.0.1",
                               "64:ff9b::a00:1","168.63.129.16"])
def test_url_rejects_nonpublic_addresses(monkeypatch, ip):
    monkeypatch.setattr(safe_fetch.socket, "getaddrinfo", lambda *a, **k: dns_addresses(ip))
    with pytest.raises(ETLRejected, match="public"):
        safe_fetch.fetch_public_data("https://example.org/data")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://user:password@example.org/x", "https://example.org:444/x", "https://example.org\\@127.0.0.1/"])
def test_url_rejects_unsupported_targets(url):
    with pytest.raises(ETLRejected): safe_fetch.checked_target(url)


def test_public_url_pins_ip_preserves_hostname_and_checks_redirect(monkeypatch):
    monkeypatch.setattr(safe_fetch.socket,"getaddrinfo", lambda host,*a,**k: dns_addresses("127.0.0.1" if host == "private.example" else "8.8.8.8"))
    calls = []
    class Response:
        status = 200
        headers = {"Content-Type":"application/json"}
        chunks = [b'[{"id":1}]',b'']
        def read1(self, *a, **k): return self.chunks.pop(0)
        def close(self): pass
    response = Response()
    class Pool:
        def __init__(self, host, **kwargs): calls.append((host, kwargs))
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def urlopen(self,method,target,**kwargs):
            calls.append((method,target,kwargs)); return response
    monkeypatch.setattr(safe_fetch.urllib3,"HTTPSConnectionPool",Pool)
    assert safe_fetch.fetch_public_data("https://example.org/data")[0] == b'[{"id":1}]'
    assert calls[0][0] == "8.8.8.8"
    assert calls[0][1]["server_hostname"] == calls[0][1]["assert_hostname"] == "example.org"
    assert calls[1][2]["headers"]["Host"] == "example.org"
    assert calls[1][2]["redirect"] is False and calls[1][2]["retries"] is False
    response.status=302
    response.headers={"Location":"https://private.example/secret"}
    with pytest.raises(ETLRejected,match="public"):
        safe_fetch.fetch_public_data("https://example.org/data")
    response.status=200
    response.headers={"Content-Length":"1000001"}
    with pytest.raises(ETLRejected,match="1 MB"):
        safe_fetch.fetch_public_data("https://example.org/data")
    response.headers={"Content-Encoding":"gzip"}
    with pytest.raises(ETLRejected,match="Compressed"):
        safe_fetch.fetch_public_data("https://example.org/data")
    response.headers={}
    response.chunks=[b"a" * 1_000_001, b""]
    with pytest.raises(ETLRejected,match="1 MB"):
        safe_fetch.fetch_public_data("https://example.org/data")


def test_url_blocks_mixed_dns_public_and_private(monkeypatch):
    monkeypatch.setattr(safe_fetch.socket,"getaddrinfo",lambda *a,**k:dns_addresses("8.8.8.8","10.0.0.1"))
    with pytest.raises(ETLRejected): safe_fetch.checked_target("https://example.org/data")


def test_csv_formula_escaping_preserves_original_for_next_transform(tmp_path, monkeypatch):
    monkeypatch.setattr(etl_tools,"ARTIFACT_ROOT",tmp_path)
    result = etl_tools._save(pd.DataFrame({"text":["=1+1", "ordinary"]}),"source","csv","test","test")
    path,_=etl_tools.artifact_info(result["artifact"]["id"])
    assert "'=1+1" in path.read_text()
    original = pd.read_json(path.with_suffix(".records"),orient="table")
    assert original["text"].tolist() == ["=1+1","ordinary"]


def test_query_context_reads_live_values_and_caches(monkeypatch):
    from utils import database
    database._CONTEXT_CACHE.clear()
    calls, sessions = [], []
    class Cursor:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def execute(self,query,params=None): calls.append((query,params))
        def fetchall(self):
            if calls[-1][0] == database.DESCRIBE_DEMO_TABLES_SQL:
                return [("payments","payment_id","integer"),("payments","payment_method","text")]
            return [("credit_card",),("paypal",),("debit_card",)]
    class Connection:
        def cursor(self): return Cursor()
        def set_session(self,**kwargs): sessions.append(kwargs)
        def rollback(self): pass
        def close(self): pass
    monkeypatch.setattr(database.psycopg2,"connect",lambda **kwargs:Connection())
    instance=database.DatabaseUtil({"dbname":"sample-context"})
    context=instance.query_context()
    assert context["values"]["payments.payment_method"] == ["credit_card","debit_card","paypal"]
    assert "payment_id (integer)" in context["schema"]
    assert sessions == [{"readonly":True,"autocommit":False}]
    assert instance.query_context() == context
    assert len(sessions) == 1
    database._CONTEXT_CACHE.clear()


def test_plain_active_users_request_is_sql(monkeypatch):
    import agents.data_agent as module
    class Analyst:
        def invoke(self,state):
            assert state["user_question"] == "Filter active users"
            assert not state["export_mode"]
            return {"is_safe":"Yes","final_answer":"One user","generated_sql_query":"SELECT user_id FROM public.users WHERE is_active = TRUE",
                    "sql_query_execution_result":{"columns":["user_id"],"rows":[[1]],"truncated":False}}
    monkeypatch.setattr(module,"sql_analyst",Analyst())
    result=TestClient(app).post("/api/chat",json={"message":"Filter active users"}).json()
    assert result["route"] == "sql" and result["status"] == "completed"


def test_url_filters_before_export_cap_and_reports_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(etl_tools,"ARTIFACT_ROOT",tmp_path)
    records=[{"id":i,"keep":i>=600} for i in range(1200)]
    monkeypatch.setattr(safe_fetch,"fetch_public_data",lambda url:(json.dumps(records).encode(),"application/json","public URL example.org"))
    monkeypatch.setattr(transformations,"plan_transform",lambda message,df:TransformPlan.model_validate({"steps":[
        {"operation":"filter","filters":[{"column":"keep","operator":"eq","values":[True]}]}]}))
    result=etl_tools.ETLTools().extract_url("https://example.org/data","keep true")
    assert result["artifact"]["row_count"] == 500
    assert result["artifact"]["source_truncated"] is True
    assert result["table"]["rows"][0][0] == 600
    assert "600 rows matched" in result["answer"]
    with pytest.raises(ETLRejected,match="5,000"):
        parse_data(json.dumps([{"id":i} for i in range(5001)]).encode(),"application/json")
