# AI Data Agent: UI and API Contract for the First Public Demo

**Status:** proposed contract to implement alongside [the deployment plan](./AI_DATA_AGENT_DEPLOYMENT_PLAN.md). Field names can change deliberately before frontend integration; freeze them once M2 tests pass.

## User journey

1. Open the demo and see sample tasks for a SQL question, an approved API extraction, and a controlled flat-file transformation.
2. Submit a prompt. The browser calls `POST /api/chat` once and waits; no conversation history is sent in v1.
3. See a short answer, route badge, and either tabular SQL results or an ETL preview and download.
4. Optionally open “How it worked” for the generated SELECT statement, validation outcome, and operation metadata.
5. See a clear, nontechnical message if the request is unsupported, blocked, busy, timed out or temporarily unavailable.

Suggested real fixture prompts after confirming schema and tools:

- SQL: “What is the average fare by ride status?”
- SQL: “How many payments used each payment method?”
- ETL: “Filter the demo ratings to ratings of at least 4.5 and export CSV.”
- ETL: “Summarize the demo payments by payment method and export CSV.”
- ETL: “Extract the approved Pokémon API data to CSV.”

The Pokémon example is available only after the backend maps a fixed source name to a server-owned endpoint with a timeout, byte/row cap, redirect rejection and JSON-shape validation. The user and model cannot supply the URL. If it is unavailable, show the failure or clearly label a bundled fixture result. Do not suggest filtering `rides.csv` by a `rating` column; the uploaded `rides.csv` header has no such column.

## ETL storage boundary

For v1, **extract → transform → load means writing a flat-file artifact** (`.csv`, newline-delimited `.json`, or `.parquet`) under a server-controlled temporary directory. The ETL branch does not write, update, create tables in, or otherwise load data into Neon/PostgreSQL. Its tools do not accept DB credentials or connection objects. Only the SQL branch reads the demo database, using a SELECT-only role. A separate guarded seed job owns initial demo-data loading before deployment.

## Endpoints

| Route | Purpose | Notes |
| --- | --- | --- |
| `GET /api/health` | Liveness: `{"status":"ok"}`. | No DB/LLM calls, cheap enough for hosting checks. |
| `POST /api/chat` | Validate a single natural-language request and invoke the router graph. | No SQL, path, code, or URL parameters from the browser. |
| `GET /api/artifacts/{artifact_id}` | Download a temporary allowlisted output. | Server maps opaque ID to metadata/path; do not accept paths. Expired/missing IDs return 404/410. |

### Request

```json
{"message":"What is the average fare by ride status?"}
```

Rules: trim whitespace, reject empty text and messages over a chosen limit (suggestion: 1,000 characters); use server settings for limits, not client-supplied values. One request is one independent invocation. Preserve a request ID in response headers and JSON for support/debugging.

### Completed SQL result

```json
{
  "request_id": "opaque-id",
  "status": "completed",
  "route": "sql",
  "answer": "Completed rides have an average fare of ...",
  "table": {
    "columns": ["status", "average_fare"],
    "rows": [["completed", 24.75]],
    "truncated": false
  },
  "artifact": null,
  "details": {
    "sql": "SELECT status, AVG(fare) AS average_fare FROM public.rides GROUP BY status LIMIT 10",
    "sql_safe": true,
    "operation": null
  },
  "error": null
}
```

Numbers above illustrate the shape, not a verified result from the CSVs. Serialize dates, decimals and other DB values consistently. The API must obtain the table from a structured DB result, not parse the current `str(list_of_tuples)` output.

### Completed ETL result

```json
{
  "request_id": "opaque-id",
  "status": "completed",
  "route": "etl",
  "answer": "Filtered the demo ratings and created a CSV file.",
  "table": {
    "columns": ["rating", "count"],
    "rows": [[5, 42]],
    "truncated": true
  },
  "artifact": {
    "id": "opaque-artifact-id",
    "name": "filtered_ratings.csv",
    "format": "csv",
    "size_bytes": 1024,
    "download_url": "/api/artifacts/opaque-artifact-id",
    "expires_at": "2026-09-23T20:00:00Z"
  },
  "details": {
    "sql": null,
    "sql_safe": null,
    "operation": "filter_ratings"
  },
  "error": null
}
```

The values illustrate a shape only. The backend owns the URL and prevents one visitor from guessing another visitor's artifact ID. Avoid putting the absolute filesystem path or raw model tool result in JSON.

### Blocked/failed result

```json
{
  "request_id": "opaque-id",
  "status": "blocked",
  "route": "sql",
  "answer": "I can only run read-only questions on the demo database.",
  "table": null,
  "artifact": null,
  "details": {"sql": null, "sql_safe": false, "operation": null},
  "error": {"code": "UNSAFE_QUERY", "message": "Try asking a question about the demo data."}
}
```

Use a documented `status` enum such as `completed | blocked | unsupported | failed`. HTTP 200 can carry a deliberate blocked/unsupported result; use 422 for invalid requests, 429 for rate limits, 503 for unavailable dependencies and 504 for a deadline. Do not send exception strings, full schema prompts, API keys or traces to the browser. The UI should use `status` and `error.code`, not parse the answer text.

## Agent adapter mapping

| Existing value | API mapping | Work required |
| --- | --- | --- |
| Outer `route_response` | `route` | Validate `sql` or `etl`; classify unexpected value as an internal failure. |
| SQL branch state currently appended to outer `messages` | `answer`, `details.sql`, `details.sql_safe`, `table` | Find the nested branch result robustly, or add a typed branch-result field. Replace stringified DB rows with a structured bounded result. |
| ETL branch state currently appended to outer `messages` | `answer`, `details.operation`, `artifact`, optional preview | Read final assistant text from branch messages, but construct artifact from trusted tool metadata; do not scrape an arbitrary filename from assistant prose. An approved API extraction or transformation always produces a flat-file output, never a DB mutation. |
| Judge's `is_safe` and comments | Optional display signal | The actual enforcement result must come from SQL parser + database restrictions; if either rejects, return `blocked`. |

## Frontend components and states

| Component | Content/behavior |
| --- | --- |
| `AppHeader` | Name, source/architecture links, small health indicator. |
| `PromptExamples` | Verified SQL and ETL prompts; buttons populate composer. |
| `Conversation` | User entries and typed assistant results; label independent requests. |
| `ResultTable` | Accessible column headers, bounded rows, empty result treatment, horizontal scroll on phones. |
| `ArtifactCard` | Name, format, size, expiry hint and download link. |
| `TechnicalDetails` | Collapsed SELECT and validation/operation summary; copy SQL if useful. |
| `PromptComposer` | Multiline input, enter/submit semantics, pending/disabled state. |
| `StatusNotice` | Cold start, service failure, blocked, unsupported, timeout, rate limit and expired artifact. |

Frontend state: `idle → submitting → completed | blocked | unsupported | failed`. Preserve each finished request in browser memory only. Disable duplicate submits while one request is pending in v1. Render model text as text/escaped Markdown without raw HTML.

## Acceptance scenarios

| Scenario | Expected behavior |
| --- | --- |
| Safe aggregate SQL prompt | `route=sql`, answer + bounded table, read-only SELECT visible in details. |
| Prompt asking to delete a table | No write; `status=blocked`, helpful message; confirm table still exists with integration test. |
| Unexpected SQL generated despite read-only request | Deterministic validator rejects before DB; DB role also denies writes. |
| Supported ETL prompt | `route=etl`, deterministic transform, unique temporary artifact and valid download. |
| Approved API extraction prompt | Bounded fetch from fixed server endpoint, validated shape, flat-file artifact and preview; no DB call. |
| Prompt with arbitrary URL, filename, SQL write request or Python code | Unsupported/blocked; no arbitrary fetch, DB write, arbitrary file read/write or code execution. |
| Two simultaneous ETL requests | Different artifact IDs; neither response downloads the other's file. |
| Expired artifact | Explicit expired/missing response; no path exposure. |
| Blank, huge or malformed request | 422 with a usable field-level message. |
| LLM/DB outage or cold start | Clear status in UI; no secrets or stack trace in response. |
| Phone and keyboard use | Composer, details, results and download all usable. |

## Source files to touch first

1. `utils/database.py` and `agents/data_agent.py`: remove import-time effects.
2. `utils/llm_pick.py`: tested/configurable OpenAI models.
3. `utils/database.py` and `agents/sql_analyst.py`: enforced read-only SQL and structured rows.
4. `utils/etl_tools.py` and `agents/etl_analyst.py`: public-safe typed ETL tools.
5. `api.py`, DTO/adapter modules, then `frontend/`.

The file guide should be updated once new modules and final contracts exist.
