"""Regressions for choosing a CSV after a successful database export."""

import csv
import io
import time
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api import app
from utils import etl_tools, transformations
from utils.source_selection import select_source
from utils.transformations import TransformPlan


@pytest.mark.parametrize("followup,mode,expected_ids", [
    ("filter payment method of apple pay from query_export.csv", "auto", []),
    ("filter payment method of apple pay from query_export.csv", "artifact", []),
    ("filter payment method of apple pay", "artifact", []),
    ("filter payment amount above 100", "artifact", [2]),
    ("filter payment amount above 100 from query_export.csv", "auto", [2]),
])
def test_database_export_then_selected_file_filter_never_requeries_db(tmp_path, monkeypatch, caplog, followup, mode, expected_ids):
    import agents.data_agent as router
    import agents.sql_analyst as sql
    monkeypatch.setattr(etl_tools, "ARTIFACT_ROOT", tmp_path)
    reads = []
    class Database:
        def __init__(self):
            assert len(reads) == 0, "A file transformation must not instantiate the DB client"
        def query_context(self):
            return {"schema": "public.payments(payment_id integer, payment_method text, amount numeric)",
                    "columns": {"payments": {"payment_id": "integer", "payment_method": "text", "amount": "numeric"}},
                    "values": {"payments.payment_method": ["paypal", "apple_pay"]}}
        def execute_sql(self, query, max_rows):
            reads.append(query)
            assert "paypal" in query and max_rows == 500
            return {"columns": ["payment_id", "payment_method", "amount"],
                    "rows": [[1, "paypal", 80], [2, "paypal", 140]], "truncated": False}
    class Model:
        def with_structured_output(self, schema): return self
        def invoke(self, prompt):
            if "Review this PostgreSQL" in prompt:
                return SimpleNamespace(answer="Yes", comments="Read only")
            return SimpleNamespace(content="SELECT payment_id, payment_method, amount FROM public.payments WHERE payment_method = 'paypal'")
    monkeypatch.setattr(sql, "DatabaseUtil", Database)
    monkeypatch.setattr(sql, "pick_llm", lambda _: Model())
    monkeypatch.setattr(router, "llm_router", SimpleNamespace(invoke=lambda _: pytest.fail("No router model needed")))
    monkeypatch.setattr(router, "etl_analyst", SimpleNamespace(invoke=lambda _: pytest.fail("No legacy tool fallback")))
    client = TestClient(app)
    original = client.post("/api/chat", json={"message": "filter payment by payment method of paypal and extract to csv"}).json()
    assert original["status"] == "completed", original
    artifact = original["artifact"]
    assert artifact["name"] == "query_export.csv"
    def plan(message, frame):
        assert frame["payment_method"].tolist() == ["paypal", "paypal"]
        condition = ({"column": "payment_method", "operator": "eq", "values": ["apple pay"]}
                     if "apple pay" in message else {"column": "amount", "operator": "gt", "values": [100]})
        return TransformPlan.model_validate({"steps": [{"operation": "filter", "filters": [condition]}]})
    monkeypatch.setattr(transformations, "plan_transform", plan)
    with caplog.at_level("INFO"):
        response = client.post("/api/chat", json={"message": followup,
            "source_artifact_id": artifact["id"], "source_mode": mode})
    result = response.json()
    assert response.status_code == 200 and result["status"] == "completed", result
    assert result["route"] == "etl" and result["details"]["operation"] == "transform_artifact"
    assert result["details"]["source_artifact_id"] == artifact["id"]
    assert result["details"]["source_name"] == "query_export.csv"
    assert result["details"]["sql"] is None
    assert len(reads) == 1
    assert [row[0] for row in result["table"]["rows"]] == expected_ids
    assert result["artifact"]["row_count"] == len(expected_ids)
    downloaded = client.get(result["artifact"]["download_url"])
    reader = csv.DictReader(io.StringIO(downloaded.text))
    assert reader.fieldnames == ["payment_id", "payment_method", "amount"]
    assert [int(row["payment_id"]) for row in reader] == expected_ids
    assert "source_kind=artifact" in caplog.text
    if not expected_ids:
        assert "No rows in the selected file" in result["answer"]


@pytest.mark.parametrize("message,mode,kind", [
    ("filter payment amount above 100", "artifact", "artifact"),
    ("filter payment amount above 100", "auto", "database"),
    ("filter payment amount above 100 from the database", "artifact", "database"),
    ("filter payments table by amount above 100", "artifact", "database"),
    ("filter payment by paypal and extract to csv", "auto", "database"),
    ("filter payment method of apple pay from query_export.csv", "auto", "artifact"),
    ("filter payments in this file", "auto", "artifact"),
    ("extract payment as output.csv", "auto", "database"),
    ("Extract https://example.org/data.json", "artifact", "url"),
    ("How many tables are there?", "artifact", "database"),
])
def test_source_precedence(message, mode, kind):
    assert select_source(message, mode)[0] == kind


@pytest.mark.parametrize("problem", ["missing", "expired", "wrong_name"])
def test_bad_artifact_source_never_falls_back_to_database(tmp_path, monkeypatch, problem):
    import agents.data_agent as router
    monkeypatch.setattr(etl_tools, "ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(router, "sql_analyst", SimpleNamespace(invoke=lambda _: pytest.fail("No DB fallback")))
    monkeypatch.setattr(router, "etl_analyst", SimpleNamespace(invoke=lambda _: pytest.fail("No legacy fallback")))
    monkeypatch.setattr(transformations, "plan_transform", lambda *args: pytest.fail("Invalid source must be rejected before model planning"))
    request = {"message": "filter payment method of apple pay from query_export.csv", "source_mode": "artifact"}
    if problem != "missing":
        name = "another_file" if problem == "wrong_name" else "query_export"
        artifact = etl_tools._save(pd.DataFrame({"payment_method": ["paypal"]}), name, "csv", "test", "test")["artifact"]
        request["source_artifact_id"] = artifact["id"]
        if problem == "expired":
            import os
            expired = time.time() - etl_tools.TTL_SECONDS - 1
            os.utime(tmp_path / (artifact["id"] + ".meta"), (expired, expired))
    response = TestClient(app).post("/api/chat", json=request)
    assert response.status_code == 200 and response.json()["status"] == "blocked"
    expected = {"missing": "select", "expired": "expired", "wrong_name": "another_file.csv"}[problem]
    assert expected in response.json()["error"]["message"]
