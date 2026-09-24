"""Read-only access to the public demo database."""

import os
import time
import threading
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal

import psycopg2
from psycopg2 import sql
from sqlglot import exp, parse


DEMO_TABLES = frozenset({"users", "vehicles", "rides", "payments", "ratings"})
LIST_DEMO_TABLES_SQL = """SELECT table_name FROM information_schema.tables
    WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
      AND table_name = ANY(%s) ORDER BY table_name"""
COUNT_DEMO_TABLES_SQL = """SELECT COUNT(*) AS table_count FROM information_schema.tables
    WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
      AND table_name = ANY(%s)"""
DESCRIBE_DEMO_TABLES_SQL = """SELECT table_name, column_name, data_type FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = ANY(%s)
    ORDER BY table_name, ordinal_position"""
MAX_ROWS = 100
EXPORT_MAX_ROWS = 500
EXPORT_COLUMNS = {
    "users": ("user_id", "city", "province", "user_type", "signup_date", "is_active"),
    "vehicles": ("vehicle_id", "driver_id", "make", "model", "year", "color", "is_active"),
    "rides": ("ride_id", "rider_id", "driver_id", "requested_at", "distance_km", "fare", "surge_multiplier", "status"),
    "payments": ("payment_id", "ride_id", "amount", "payment_method", "payment_status"),
    "ratings": ("rating_id", "ride_id", "rating", "rated_at"),
}
EXPORT_PRIMARY_KEYS = {"users": "user_id", "vehicles": "vehicle_id", "rides": "ride_id",
                       "payments": "payment_id", "ratings": "rating_id"}
FORBIDDEN_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter,
    exp.Command, exp.Into, exp.Merge, exp.Grant, exp.Revoke,
)


class UnsafeQuery(ValueError):
    """The generated query is outside the public demo's read-only scope."""


def validate_select(query: str, max_rows: int = MAX_ROWS) -> str:
    """Accept one SELECT over demo tables and cap returned rows in SQL.

    DB permissions and a read-only transaction are the primary write boundary.
    """
    try:
        statements = parse(query, read="postgres")
    except Exception as exc:
        raise UnsafeQuery("Could not parse the generated SQL") from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise UnsafeQuery("Only one SELECT statement is allowed")
    tree = statements[0]
    if any(isinstance(node, FORBIDDEN_NODES) for node in tree.walk()):
        raise UnsafeQuery("A modifying SQL construct was blocked")
    if max_rows not in {MAX_ROWS, EXPORT_MAX_ROWS}:
        raise UnsafeQuery("Invalid result limit")
    if tree.args.get("locks"):
        raise UnsafeQuery("Row locking is not allowed")
    cte_names = {cte.alias.lower() for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        schema = table.db.lower() if table.db else "public"
        if table.catalog or (not (name in cte_names and not table.db) and
                             (name not in DEMO_TABLES or schema != "public")):
            raise UnsafeQuery("Only public demo tables may be queried")
    if any(not isinstance(star.parent, exp.Count) for star in tree.find_all(exp.Star)):
        raise UnsafeQuery("Select named columns rather than all columns")
    allowed_functions = {
        "avg", "count", "sum", "min", "max", "round", "coalesce",
        "date_trunc", "timestamp_trunc", "extract", "lower", "upper", "nullif", "cast",
        "replace", "trim", "length", "substring", "concat", "abs",
        "and", "or", "case", "if",
    }
    for function in tree.find_all(exp.Func):
        if isinstance(function.parent, exp.Dot):
            raise UnsafeQuery("Schema-qualified functions are not permitted")
        if function.sql_name().lower() not in allowed_functions:
            raise UnsafeQuery("This SQL function is not available in the demo")
    return f"SELECT * FROM ({tree.sql(dialect='postgres')}) AS demo_result LIMIT {max_rows + 1}"


_CONTEXT_CACHE = {}
_CONTEXT_LOCK = threading.Lock()
CONTEXT_TTL = 300


def _json_value(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return value


def connection_settings() -> dict:
    """Read settings only when needed; no network activity during import."""
    return {
        "host": os.environ.get("DB_HOST") or os.environ["neon_hostname"],
        "port": int(os.environ.get("DB_PORT") or os.environ.get("port", "5432")),
        "dbname": os.environ.get("DB_NAME") or os.environ["database"],
        "user": os.environ.get("DB_USER") or os.environ["user"],
        "password": os.environ.get("DB_PASSWORD") or os.environ["password"],
        "sslmode": "require",
        "connect_timeout": 8,
    }


class DatabaseUtil:
    def __init__(self, db_config=None):
        self.db_config = db_config or connection_settings()

    def schema_details(self, schema_name="public"):
        """Return column names/types, not sample rows containing personal fields."""
        if schema_name != "public":
            raise ValueError("Only the public demo schema is supported")
        lines = ["Demo schema (read-only):"]
        with closing(psycopg2.connect(**self.db_config)) as connection:
            connection.set_session(readonly=True, autocommit=False)
            with connection.cursor() as cursor:
                for table in sorted(DEMO_TABLES):
                    cursor.execute(
                        """SELECT column_name, data_type FROM information_schema.columns
                           WHERE table_schema = 'public' AND table_name = %s
                           ORDER BY ordinal_position""", (table,)
                    )
                    columns = cursor.fetchall()
                    lines.append(f"Table: public.{table}")
                    lines.extend(f"  {name}: {kind}" for name, kind in columns)
            connection.rollback()
        return "\n".join(lines)

    def query_context(self) -> dict:
        """Bounded schema and value examples from the connected demo DB, cached 5 minutes."""
        key = tuple(sorted(self.db_config.items()))
        with _CONTEXT_LOCK:
            cached = _CONTEXT_CACHE.get(key)
            if cached and time.monotonic() - cached[0] < CONTEXT_TTL:
                return cached[1]
        columns, values, lines = {}, {}, []
        with closing(psycopg2.connect(**self.db_config)) as connection:
            connection.set_session(readonly=True, autocommit=False)
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '4000ms'")
                cursor.execute(DESCRIBE_DEMO_TABLES_SQL, (sorted(DEMO_TABLES),))
                for table, name, kind in cursor.fetchall():
                    columns.setdefault(table, {})[name] = kind
                for table, fields in columns.items():
                    lines.append(f"Table public.{table}: " + ", ".join(f"{k} ({v})" for k, v in fields.items()))
                    text_fields = [name for name, kind in fields.items()
                                   if kind in {"text", "character varying", "character", "USER-DEFINED", "boolean"}]
                    if not text_fields:
                        continue
                    cursor.execute(sql.SQL("SELECT {} FROM public.{} LIMIT 256").format(
                        sql.SQL(", ").join(sql.SQL("LEFT({}::text, 100)").format(sql.Identifier(name))
                                          for name in text_fields), sql.Identifier(table)))
                    rows = cursor.fetchall()
                    for i, name in enumerate(text_fields):
                        values[f"{table}.{name}"] = sorted({str(row[i]) for row in rows if row[i] is not None})[:24]
            connection.rollback()
        context = {"schema": "\n".join(lines), "columns": columns, "values": values}
        with _CONTEXT_LOCK:
            if len(_CONTEXT_CACHE) >= 8:
                _CONTEXT_CACHE.clear()
            _CONTEXT_CACHE[key] = (time.monotonic(), context)
        return context

    def demo_metadata(self, kind: str, table: str | None = None) -> dict:
        """Run only fixed catalog lookups on existing allowlisted public tables."""
        if kind not in {"tables", "table_count", "schema"} or (table is not None and table not in DEMO_TABLES):
            raise UnsafeQuery("Only public demo table metadata is available")
        if kind in {"tables", "table_count"} and table is not None:
            raise UnsafeQuery("A table name is not needed to list demo tables")

        names = [table] if table else sorted(DEMO_TABLES)
        query = {"tables": LIST_DEMO_TABLES_SQL, "table_count": COUNT_DEMO_TABLES_SQL,
                 "schema": DESCRIBE_DEMO_TABLES_SQL}[kind]
        with closing(psycopg2.connect(**self.db_config)) as connection:
            connection.set_session(readonly=True, autocommit=False)
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '8000ms'")
                cursor.execute(query, (names,))
                rows = cursor.fetchall()
            connection.rollback()
        return {
            "columns": (["table_name"] if kind == "tables" else ["table_count"] if kind == "table_count"
                        else ["table_name", "column_name", "data_type"]),
            "rows": [list(row) for row in rows],
            "truncated": False,
        }

    def export_demo_table(self, table: str) -> dict:
        """Read at most 500 rows of approved fields for a flat-file export."""
        if table not in EXPORT_COLUMNS:
            raise UnsafeQuery("Only approved demo tables can be exported")
        columns = EXPORT_COLUMNS[table]
        # Table, columns and sort key come only from constants above.
        query = (f"SELECT {', '.join(columns)} FROM public.{table} "
                 f"ORDER BY {EXPORT_PRIMARY_KEYS[table]} LIMIT %s")
        with closing(psycopg2.connect(**self.db_config)) as connection:
            connection.set_session(readonly=True, autocommit=False)
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '8000ms'")
                cursor.execute(query, (EXPORT_MAX_ROWS + 1,))
                rows = cursor.fetchall()
            connection.rollback()
        return {"columns": list(columns), "rows": rows[:EXPORT_MAX_ROWS],
                "truncated": len(rows) > EXPORT_MAX_ROWS}

    def execute_sql(self, query, max_rows=MAX_ROWS):
        checked = validate_select(query, max_rows)
        with closing(psycopg2.connect(**self.db_config)) as connection:
            connection.set_session(readonly=True, autocommit=False)
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '8000ms'")
                cursor.execute(checked)
                columns = [column.name for column in cursor.description]
                records = cursor.fetchmany(max_rows + 1)
            connection.rollback()
        return {
            "columns": columns,
            "rows": [[_json_value(value) for value in record] for record in records[:max_rows]],
            "truncated": len(records) > max_rows,
        }
