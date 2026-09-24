"""Bounded, flat-file-only ETL for anonymous portfolio visitors."""

import json
import os
import re
import time
import uuid
from pathlib import Path

import pandas as pd
import requests
from utils.database import DatabaseUtil, DEMO_TABLES
from utils.errors import ETLRejected
from utils.session_context import session_id as current_session_id

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_ROOT = Path(os.environ.get("DATA_ARTIFACT_DIR", ROOT / "data" / "artifacts")).resolve()
FORMATS = {"csv", "json", "parquet"}
TTL_SECONDS = 3600
MAX_BYTES = 256_000
MAX_ROWS = 500
POKEMON_URL = "https://pokeapi.co/api/v2/pokemon?limit=20"
DEMO_TABLE_ALIASES = {
    "user": "users", "users": "users", "vehicle": "vehicles", "vehicles": "vehicles",
    "ride": "rides", "rides": "rides", "payment": "payments", "payments": "payments",
    "transaction": "payments", "transactions": "payments", "rating": "ratings", "ratings": "ratings",
}


def referenced_demo_tables(message: str) -> set[str]:
    """Recognize singular/plural source nouns without rewriting user values."""
    return {DEMO_TABLE_ALIASES[word.lower()] for word in re.findall(r"\b[a-z]+\b", message, re.I)
            if word.lower() in DEMO_TABLE_ALIASES}


DB_EXPORT_RE = re.compile(
    r"(?:extract|export|download|save) (?:the )?(?:public\.)?(?P<table>[a-z_]+)(?: table)?"
    r"(?: (?:from|in) (?:the )?(?:db|database|neon|neondb))?"
    r"(?: (?:as|to|into) (?:a )?csv(?: file)?)?"
    r"(?: and (?:save|export) (?:it )?(?:as|to) (?:a )?csv(?: file)?)?",
    re.IGNORECASE,
)
ACTIVE_USERS_RE = re.compile(
    r"(?:filter|keep|show|transform) (?:the )?active users"
    r"(?: (?:from|in) (?:the )?(?:exported )?users(?: csv| file| export)?)?"
    r"(?: (?:as|to|into) (?:a )?csv(?: file)?)?", re.IGNORECASE,
)
USERS_BY_PROVINCE_RE = re.compile(
    r"(?:summarize|group|count|transform) (?:the )?users by province"
    r"(?: (?:from|in) (?:the )?(?:exported )?users(?: csv| file| export)?)?"
    r"(?: (?:as|to|into) (?:a )?csv(?: file)?)?", re.IGNORECASE,
)


def demo_etl_request(message: str) -> tuple[str, str | None] | None:
    normalized = " ".join(message.strip().split()).rstrip("?.!")
    export = DB_EXPORT_RE.fullmatch(normalized)
    if export and export.group("table").lower() in DEMO_TABLE_ALIASES:
        return "extract_demo_table", DEMO_TABLE_ALIASES[export.group("table").lower()]
    if ACTIVE_USERS_RE.fullmatch(normalized):
        return "filter_active_users", None
    if USERS_BY_PROVINCE_RE.fullmatch(normalized):
        return "summarize_users_by_province", None
    return None


def requests_database_etl(message: str) -> bool:
    """A database source plus a request for a file belongs to the ETL export path."""
    source = referenced_demo_tables(message) or re.search(r"\b(?:db|database|neondb|table)\b", message, re.I)
    export = re.search(r"\b(?:extract|export|download|save|csv|parquet|jsonl)\b", message, re.I)
    return bool(source and export)


def requests_artifact_transform(message: str) -> bool:
    return bool(artifact_reference_name(message) or re.search(r"\b(?:this|that|exported|previous|last|result|downloaded)\b.*\b(?:data|file|csv|export|result)\b"
                          r"|\b(?:transform|filter|sort|group|rename|deduplicate|convert)\b.*\b(?:it|this|that)\b", message, re.I))


def artifact_reference_name(message: str) -> str | None:
    """An input filename is a label for the selected ID, never a server file path."""
    match = re.search(r"\b(?:from|in|on|using|within)\s+(?:(?:the|selected|file)\s+)*"
                      r"[`\"']?([\w-]+(?:[.][\w-]+)*\.(?:csv|jsonl|json|parquet))\b", message, re.I)
    return match.group(1) if match else None


def output_format(message: str) -> str:
    match = re.search(r"\b(?:to|as|into) (?:a )?(csv|jsonl|json(?: lines)?|parquet)\b", message, re.I)
    fmt = match.group(1).lower() if match else "csv"
    return "json" if fmt.startswith("json") else fmt


def _artifact_path(artifact_id: str) -> Path:
    try:
        safe_id = str(uuid.UUID(artifact_id))
    except ValueError as exc:
        raise ETLRejected("Invalid artifact ID") from exc
    path = (ARTIFACT_ROOT / safe_id).resolve()
    if path.parent != ARTIFACT_ROOT:
        raise ETLRejected("Invalid artifact path")
    return path


def artifact_info(artifact_id: str):
    base = _artifact_path(artifact_id)
    manifest = base.with_suffix(".meta")
    if not manifest.is_file() or manifest.is_symlink() or time.time() - manifest.stat().st_mtime > TTL_SECONDS:
        raise FileNotFoundError("Artifact expired or not found")
    metadata = json.loads(manifest.read_text())
    session_id = current_session_id.get()
    if session_id is not None and metadata.get("session_id") is None:
        metadata["session_id"] = session_id
        manifest.write_text(json.dumps(metadata))
    if session_id is not None and metadata.get("session_id") != session_id:
        raise FileNotFoundError("Artifact does not belong to this session")
    path = base.with_suffix("." + metadata["format"])
    if metadata["format"] not in FORMATS or not path.is_file() or path.is_symlink() or path.stat().st_size > 2_000_000:
        raise FileNotFoundError("Artifact unavailable")
    return path, metadata


def delete_session_artifacts(session_id: str) -> None:
    """Delete every artifact owned by an expired anonymous session."""
    if not ARTIFACT_ROOT.is_dir():
        return
    for manifest in ARTIFACT_ROOT.glob("*.meta"):
        if manifest.is_symlink():
            continue
        try:
            metadata = json.loads(manifest.read_text())
        except (OSError, ValueError):
            continue
        if metadata.get("session_id") != session_id:
            continue
        for suffix in (".csv", ".json", ".parquet", ".meta", ".records"):
            candidate = manifest.with_suffix(suffix)
            if candidate.is_file() and not candidate.is_symlink():
                candidate.unlink(missing_ok=True)


def _prune_expired():
    """Bound local scratch growth; a restart may remove files sooner."""
    for manifest in ARTIFACT_ROOT.glob("*.meta"):
        if manifest.is_symlink() or time.time() - manifest.stat().st_mtime <= TTL_SECONDS:
            continue
        for suffix in (".csv", ".json", ".parquet", ".meta", ".records"):
            candidate = manifest.with_suffix(suffix)
            if candidate.is_file() and not candidate.is_symlink():
                candidate.unlink(missing_ok=True)


def _save(df: pd.DataFrame, name: str, fmt: str, operation: str, source: str, source_truncated=False):
    if fmt not in FORMATS:
        raise ETLRejected("Choose CSV, JSON, or Parquet")
    if len(df) > MAX_ROWS:
        raise ETLRejected("Too many output rows")
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    _prune_expired()
    artifact_id = str(uuid.uuid4())
    path = _artifact_path(artifact_id).with_suffix("." + fmt)
    escaped_cells = 0
    if fmt == "csv":
        safe = df.copy()
        def escape_cell(value):
            nonlocal escaped_cells
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                escaped_cells += 1
                return "'" + value
            return value
        for column in safe.columns:
            safe[column] = safe[column].map(escape_cell)
        safe.columns = [escape_cell(str(column)) for column in safe.columns]
        safe.to_csv(path, index=False)
    elif fmt == "json":
        df.to_json(path, orient="records", lines=True)
    else:
        df.to_parquet(path, index=False)
    if path.stat().st_size > 2_000_000:
        path.unlink()
        raise ETLRejected("Output exceeds the demo size limit")
    records = path.with_suffix(".records")
    df.to_json(records, orient="table", date_format="iso")
    if records.stat().st_size > 2_000_000:
        path.unlink()
        records.unlink()
        raise ETLRejected("Output exceeds the demo size limit")
    metadata = {"id": artifact_id, "name": f"{name}.{fmt}", "format": fmt,
                "size_bytes": path.stat().st_size, "source": source,
                "row_count": len(df), "source_truncated": source_truncated,
                "session_id": current_session_id.get()}
    _artifact_path(artifact_id).with_suffix(".meta").write_text(json.dumps(metadata))
    preview = df.head(10).where(pd.notna(df.head(10)), None)
    answer = f"Created {name}.{fmt} with {len(df)} rows from {source}."
    if source_truncated:
        answer += " This is a limited extract; subsequent transformations use only the exported rows."
    if escaped_cells:
        answer += f" Escaped {escaped_cells} spreadsheet-formula cells in the CSV download."
    return {
        "answer": answer,
        "operation": operation,
        "table": {"columns": list(df.columns), "rows": json.loads(preview.to_json(orient="values", date_format="iso")),
                  "truncated": len(df) > 10},
        "artifact": {**metadata, "download_url": f"/api/artifacts/{artifact_id}"},
    }


class ETLTools:
    def export_query_result(self, data: dict, query: str, message: str):
        df = pd.DataFrame(data["rows"], columns=data["columns"])
        # Joins can produce duplicate output names. Require SQL aliases instead.
        if df.columns.duplicated().any():
            raise ETLRejected("The query returned duplicate column names. Ask for distinct column aliases.")
        result = _save(df, "query_export", output_format(message), "export_filtered_query",
                       "demo database query", data["truncated"])
        result["sql"] = query
        if data["truncated"]:
            result["answer"] += " Export is limited to the first 500 matching rows."
        return result

    def extract_url(self, url: str, message: str):
        from utils.safe_fetch import fetch_public_data
        from utils.data_sources import parse_data
        from utils.transformations import plan_transform, apply_plan
        body, content_type, source = fetch_public_data(url)
        path_match = re.search(r"records path:\s*([\w.-]+)", message, re.I)
        df = parse_data(body, content_type, path_match.group(1) if path_match else None)
        plan = plan_transform(message, df)
        output = apply_plan(df, plan)
        result = _save(output.head(MAX_ROWS), "url_data", plan.format, "extract_transform_url",
                       source, len(output) > MAX_ROWS)
        if len(output) > MAX_ROWS:
            result["answer"] += f" {len(output)} rows matched; exported the first 500."
        return result

    def transform_artifact(self, source_artifact_id: str | None, message: str):
        from utils.transformations import plan_transform, apply_plan
        if not source_artifact_id:
            raise ETLRejected("Extract or export a dataset first, then select it as the transformation source.")
        try:
            path, info = artifact_info(source_artifact_id)
        except FileNotFoundError:
            raise ETLRejected("The selected source has expired. Extract or export it again.") from None
        named_source = artifact_reference_name(message)
        if named_source and named_source.casefold() != info["name"].casefold():
            raise ETLRejected(f"The request names {named_source}, but the selected file is {info['name']}. Select the named file first.")
        records = path.with_suffix(".records")
        if records.is_file() and not records.is_symlink() and records.stat().st_size <= 2_000_000:
            df = pd.read_json(records, orient="table")
        elif info["format"] == "csv":
            df = pd.read_csv(path, nrows=MAX_ROWS + 1)
        elif info["format"] == "json":
            df = pd.read_json(path, orient="records", lines=True)
        else:
            df = pd.read_parquet(path)
        if len(df) > MAX_ROWS:
            raise ETLRejected("Artifact exceeds the row limit.")
        plan = plan_transform(message, df)
        output = apply_plan(df, plan)
        result = _save(output.head(MAX_ROWS), "transformed_data", plan.format, "transform_artifact",
                       "selected " + info["name"], info.get("source_truncated", False) or len(output) > MAX_ROWS)
        result["source_artifact_id"] = source_artifact_id
        result["source_name"] = info["name"]
        if output.empty:
            result["answer"] += " No rows in the selected file matched the transformation."
        return result

    def extract_demo_table(self, table: str):
        if table not in DEMO_TABLES:
            raise ETLRejected("Only the five approved demo tables can be exported")
        data = DatabaseUtil().export_demo_table(table)
        df = pd.DataFrame(data["rows"], columns=data["columns"])
        result = _save(df, f"{table}_export", "csv", "extract_demo_table",
                       f"demo database public.{table}", data["truncated"])
        if data["truncated"]:
            result["answer"] += " Export is limited to the first 500 rows."
        return result

    def transform_users_export(self, source_artifact_id: str | None, operation: str):
        if operation not in {"filter_active_users", "summarize_users_by_province"}:
            raise ETLRejected("Unsupported transformation")
        if not source_artifact_id:
            raise ETLRejected("Export the users table to CSV first, then request the transformation.")
        try:
            source_path, info = artifact_info(source_artifact_id)
        except (FileNotFoundError, ETLRejected):
            raise ETLRejected("The users export has expired. Export users again first.") from None
        if info.get("source") != "demo database public.users" or info.get("format") != "csv":
            raise ETLRejected("This transformation needs a users CSV exported from the demo database")
        df = pd.read_csv(source_path, nrows=MAX_ROWS + 1)
        required = {"user_id", "city", "province", "user_type", "signup_date", "is_active"}
        if not required.issubset(df.columns) or len(df) > MAX_ROWS:
            raise ETLRejected("The exported users CSV has an unexpected shape")
        if operation == "filter_active_users":
            active = df["is_active"].astype(str).str.lower().isin({"true", "t", "1"})
            output = df.loc[active].copy()
            name = "active_users"
        else:
            output = df.groupby("province", dropna=False).size().reset_index(name="user_count")
            name = "users_by_province"
        return _save(output, name, "csv", operation, "exported demo users CSV")

    def extract_pokemon(self, fmt="csv"):
        # The endpoint is a server constant. Never follow a redirect or accept
        # a URL, path, or DNS target supplied by the visitor or model.
        try:
            with requests.get(POKEMON_URL, timeout=(3, 8), allow_redirects=False, stream=True) as response:
                if response.status_code != 200:
                    raise ETLRejected("The approved API source is unavailable")
                size = 0
                chunks = []
                for chunk in response.iter_content(chunk_size=8192):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ETLRejected("The approved API response is too large")
                    chunks.append(chunk)
            payload = json.loads(b"".join(chunks))
            records = payload["results"]
            if not isinstance(records, list) or len(records) > 20:
                raise ValueError("Unexpected result shape")
            df = pd.DataFrame([{"name": item["name"], "url": item["url"]} for item in records])
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            raise ETLRejected("The approved API source is unavailable") from exc
        return _save(df, "pokemon", fmt, "extract_pokemon", "approved Pokémon API")

    def filter_ratings(self, min_rating=4.5, fmt="csv"):
        if not isinstance(min_rating, (int, float)) or isinstance(min_rating, bool) or not 0 <= min_rating <= 5:
            raise ETLRejected("Rating must be between 0 and 5")
        df = pd.read_csv(ROOT / "data" / "ratings.csv", usecols=["ride_id", "rating"], nrows=12000)
        result = df.loc[df["rating"] >= min_rating, ["ride_id", "rating"]].head(MAX_ROWS)
        return _save(result, "filtered_ratings", fmt, "filter_ratings", "demo ratings CSV")

    def summarize_payments(self, fmt="csv"):
        df = pd.read_csv(ROOT / "data" / "payments.csv", usecols=["payment_method", "amount"], nrows=16000)
        result = df.groupby("payment_method", as_index=False).agg(
            payment_count=("amount", "size"), total_amount=("amount", "sum")
        )
        return _save(result, "payment_summary", fmt, "summarize_payments", "demo payments CSV")
