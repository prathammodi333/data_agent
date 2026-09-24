# AI Data Agent: Current Codebase File Guide

This file reflects the implemented public-demo architecture in this repository, not the older planning notes. The authoritative sources are the FastAPI boundary in `api.py`, the browser-facing adapter in `services/agent_adapter.py`, the LangGraph router in `agents/data_agent.py`, and the product description in `README.md`.

## How to Use This Guide

Use this guide to narrow a request to the exact implementation area before editing. For most changes, read only the relevant file(s) plus the current public API contract in `api.py` and the behavior notes in `README.md`.

The current project has four practical layers:

```text
Browser / UI
    -> FastAPI boundary (api.py)
        -> LangGraph router + state (agents/data_agent.py, Models/schema.py)
            -> SQL branch or ETL branch
                -> DB / LLM / validated URL / artifact filesystem
```

The repo is already an API-first portfolio demo. Browser code should call the backend through HTTP; it should not import Python agent modules directly.

## Change-Location Map

| Requested change | Read these files first | Usually also inspect |
|---|---|---|
| Change public API request/response contract | `api.py`, `services/agent_adapter.py`, `Models/schema.py` | `README.md`, `frontend/src/App.tsx` |
| Change routing between SQL and ETL or source precedence | `agents/data_agent.py`, `utils/source_selection.py`, `utils/safe_fetch.py` | `utils/demo_metadata.py`, `utils/payment_intents.py` |
| Change SQL generation, validation, or safety | `agents/sql_analyst.py`, `utils/database.py`, `Models/schema.py` | `utils/llm_pick.py`, `utils/value_matching.py` |
| Change SQL result shape or response tables | `services/agent_adapter.py`, `agents/sql_analyst.py`, `Models/schema.py` | `frontend/src/App.tsx` |
| Add or adjust ETL behavior | `utils/etl_tools.py`, `agents/data_agent.py`, `services/agent_adapter.py` | `utils/safe_fetch.py`, `README.md` |
| Change artifact export format, size limits, or cleanup rules | `utils/etl_tools.py`, `utils/anonymous_sessions.py`, `api.py` | `README.md`, `.gitignore` |
| Change anonymous session TTL or expiry behavior | `utils/anonymous_sessions.py`, `api.py`, `frontend/src/App.tsx` | `utils/session_context.py`, `tests/test_sessions.py` |
| Change the URL fetch security policy | `utils/safe_fetch.py`, `utils/etl_tools.py` | `README.md` |
| Change model/provider configuration | `utils/llm_pick.py`, `pyproject.toml`, `README.md` | `.env.example` |
| Change database schema or demo seed data | `feed_db.py`, `data/*.csv`, `utils/database.py` | `README.md` |
| Change UI/backend startup or deployment config | `api.py`, `README.md`, `pyproject.toml`, `frontend/package.json` | `.env.example` |
| Update docs or architecture notes | `README.md`, `LLM_UI_HANDOFF.md`, this file | relevant source files |

## File-by-File Reference

### `api.py`

**Role:** Public HTTP boundary for the portfolio app.

**Actual behavior:**
- Defines `ChatRequest` with `message`, `source_artifact_id`, and `source_mode`.
- Exposes `POST /api/chat` and `GET /api/health`.
- Validates rate limiting, concurrency, and timeouts.
- Calls `services.agent_adapter.invoke_agent()` in a thread pool.
- Converts backend output to the `ChatResponse` schema.
- Serves artifact downloads via `GET /api/artifacts/{artifact_id}`.
- Creates a 15-minute anonymous session through `GET /api/session` and stores its opaque ID in an HttpOnly cookie.
- Rejects stale chat/download requests with `410 SESSION_EXPIRED`; a page reload may start a new session.

**Modify this file when:**
- Adding backend routes or validation logic.
- Changing API payloads or error formatting.
- Adjusting request throttling, timeouts, or CORS policy.

**Important:** This is the public contract of the app; keep it separate from the LangGraph implementation details.

### `utils/anonymous_sessions.py`

**Role:** Process-local anonymous session registry and expiry cleanup.

**Actual behavior:**
- Creates opaque UUID session IDs with a fixed 15-minute lifetime; activity does not extend the lifetime.
- Validates active sessions for API requests and runs a periodic cleanup sweep for idle sessions.
- Calls the artifact cleanup hook when a session expires.

**Deployment note:** The registry is process-local, matching the app's local filesystem artifact storage. A multi-instance deployment needs shared session/artifact storage or sticky routing.

### `utils/session_context.py`

**Role:** Request-local session identity propagated into synchronous graph work.

The API copies this context into the worker thread so ETL artifact creation and artifact reads use the same session ID without adding session arguments throughout the agent graph.

### `services/agent_adapter.py`

**Role:** Serialization layer between LangGraph state and the browser response.

**Important symbols:**
- `invoke_agent(message, request_id, source_artifact_id, source_mode)`
- `_invoke_agent(...)`

**Actual behavior:**
- Imports the compiled router from `agents.data_agent` lazily.
- Invokes the graph with `HumanMessage` input, source artifact metadata, and route metadata.
- Reads `route_response` and `branch_result` from the compiled graph state.
- Converts SQL or ETL branches into browser-safe `status`, `route`, `answer`, `table`, `artifact`, and `details` payloads.

**Modify this file when:**
- Changing the browser response schema.
- Changing route-specific output formatting.
- Handling new graph branches or blocked states.

### `main.py`

**Role:** Simple local smoke-test harness for the compiled graph.

**Actual behavior:** Imports `data_agent` and executes a single hard-coded example in a script context. It is not the public API.

**Modify this file when:**
- Running local manual smoke checks.
- Trying a quick example outside the server.

**Do not use this file for:**
- HTTP route definitions.
- Production request handling.

### `agents/data_agent.py`

**Role:** Top-level LangGraph router and state machine.

**Important symbols:**
- `router_node(state)`
- `etl_node(state)`
- `sql_node(state)`
- `route_edge(state)`
- `data_agent_graph`
- `data_agent`

**Actual behavior:**
- Routes requests to SQL or ETL based on the last message and source metadata.
- Applies source precedence using `select_source()` from `utils/source_selection.py`.
- Handles SQL export requests, artifact transformations, public URL extraction, and trusted database ETL requests.
- Stores `branch_result` for the adapter to serialize.

**Current flow:**

```text
START -> router_node -> sql_node OR etl_node
```

**Modify this file when:**
- Changing source precedence or graph routing.
- Adding a new branch or state flag.
- Changing how ETL requests are delegated.

### `agents/sql_analyst.py`

**Role:** SQL query generation and validation branch.

**Important symbols:**
- `known_read_query(question)`
- `curate_ques(state)`
- `show_demo_metadata(state)`
- `prompt_query_context(state)`
- `generate_sql(state)`
- `is_safe_sql(state)`
- `canceled_sql(state)`
- `execute_sql(state)`

**Actual behavior:**
- Uses deterministic queries for stable public examples like row counts and average fare by ride status.
- Uses `DatabaseUtil().query_context()` and `validate_select()` for model-generated SQL.
- Checks safety with a SQL AST validator and a model-based advisory judge.
- Executes read-only SQL only against the approved `public` demo tables.
- Stores final query result and answer in the graph state.

**Important caveat:** The whole DB boundary is enforced by `DatabaseUtil` and `validate_select()`, not only by the LLM judge.

### `agents/etl_analyst.py`

**Role:** Legacy ETL tool-calling branch.

**Actual status:** The app no longer relies on this branch as the primary ETL mechanism for public demo requests. The main ETL logic lives in `utils/etl_tools.py`, and `agents/data_agent.py` directly orchestrates artifact and export flows.

**Modify this file only when:**
- Reworking the older tool-calling ETL branch.
- Reintroducing a specialized ETL tool graph.

This file is secondary to the real ETL implementation in the tools module.

### `Models/schema.py`

**Role:** Pydantic models for agent state and LLM-structured outputs.

**Important models:**
- `AgentSchema`
- `JudgeSchema`
- `ETLAgentSchema`
- `RouterSchema`
- `DataAgentSchema`

**Actual behavior:**
- `AgentSchema` holds SQL state fields like `user_question`, `generated_sql_query`, `is_safe`, and `final_answer`.
- `DataAgentSchema` includes routing state: `route_response`, `source_artifact_id`, `source_mode`, `source_kind`, and `branch_result`.

**Modify this file when:**
- Adding graph state fields.
- Changing voter/or router output values.
- Updating the backend-facing state contract.

### `utils/database.py`

**Role:** Read-only PostgreSQL access for the demo database.

**Important symbols:**
- `UnsafeQuery`
- `validate_select(query, max_rows)`
- `connection_settings()`
- `DatabaseUtil.query_context()`
- `DatabaseUtil.demo_metadata()`
- `DatabaseUtil.export_demo_table()`
- `DatabaseUtil.execute_sql()`

**Actual behavior:**
- Restricts queries to a single `SELECT` over approved `public` tables.
- Rejects writes and disallowed SQL constructs via AST validation.
- Uses read-only transactions and fixed table/column allowlists.
- Returns `columns` and `rows` in structured dict form rather than a raw string blob.

**Modify this file when:**
- Changing the trusted demo schema behavior.
- Tightening or expanding the allowed SQL subset.
- Adjusting export row limits, transaction settings, or schema introspection.

### `utils/etl_tools.py`

**Role:** Main ETL implementation for URL, database-export, and artifact transformations.

**Important symbols:**
- `demo_etl_request(message)`
- `requests_database_etl(message)`
- `artifact_reference_name(message)`
- `artifact_info(artifact_id)`
- `delete_session_artifacts(session_id)`
- `ETLTools.export_query_result(...)`
- `ETLTools.extract_url(...)`
- `ETLTools.transform_artifact(...)`
- `ETLTools.extract_demo_table(...)`
- `ETLTools.transform_users_export(...)`

**Actual behavior:**
- Saves exported or transformed results to the allowlisted artifact directory under `data/artifacts`.
- Writes flat files only in `csv`, `json`, and `parquet` formats.
- Loads data from public URLs, DB exports, or previously created artifacts.
- Applies constrained transformation plans using pandas.
- Validates artifact IDs and enforces output row/size limits.
- Stores the owning session ID in each artifact manifest and rejects reads from other sessions.
- Deletes all companion files (`.csv`, `.json`, `.parquet`, `.records`, and `.meta`) owned by an expired session.

**Modify this file when:**
- Supporting new output formats or dataset sources.
- Tightening artifact validation or expiry rules.
- Changing transformation logic or file metadata.

### `utils/safe_fetch.py`

**Role:** Public HTTP(S) URL validation and download guard.

**Important functions:**
- `public_address(value)`
- `source_url(message)`
- `checked_target(url)`
- `fetch_public_data(url)`

**Actual behavior:**
- Accepts only public, non-private IP destinations.
- Rejects credentials, compressed responses, HTML pages, and many redirect patterns.
- Uses pinned host/IP resolution with TLS hostname verification.
- Reads only a limited amount of content before returning the payload.

**Modify this file when:**
- Tightening SSRF protections.
- Changing the supported content types or download budget.

### `utils/source_selection.py`

**Role:** Source precedence logic for route selection.

**Actual behavior:**
- Resolves whether the user request is tied to a URL, artifact, database, or unspecified source.
- Gives explicit UI file selection and explicit database-source requests precedence over loosely named table references.

**Modify this file when:**
- Changing source precedence or adding new request categories.

### `utils/llm_pick.py`

**Role:** Model selection helper.

**Important symbol:**
- `pick_llm(level)`

**Actual behavior:**
- Maps `low`, `medium`, `high`, `etl`, and `claude` to environment-configured OpenAI model names.
- Defaults to `gpt-4.1-mini`.
- Uses `temperature=0` and bounded token settings.

**Modify this file when:**
- Changing model names or provider configuration.
- Adjusting token budgets or retries.

### `feed_db.py`

**Role:** Seed script for the synthetic demo database.

**Actual behavior:**
- Creates the required tables in `public` for `users`, `vehicles`, `rides`, `payments`, and `ratings`.
- Truncates tables and loads the CSV seed data.
- Intentionally destructive; use only in a disposable demo database.

**Modify this file when:**
- Updating schema or demo data mappings.
- Rebuilding the seeded database for a new dataset.

### `README.md`

**Role:** Current product documentation and deployment guide.

**Actual behavior:**
- Describes the public demo architecture, DB setup, ETL exports, artifact flow, and deployment process.
- Represents the real product behavior more accurately than older planning docs.

**Modify this file when:**
- Changing user-facing behavior or deployment steps.
- Updating environment variables or public contract details.

### `LLM_UI_HANDOFF.md`

**Role:** Higher-level product handoff for the UI/API integration.

**Use this file for:**
- product decisions,
- deployment planning,
- UI contract details beyond the direct code-level map.

### `frontend/`

**Role:** Vite React front end that calls the API.

**Important files:**
- `frontend/src/App.tsx`
- `frontend/src/main.tsx`
- `frontend/package.json`

**Actual behavior:**
- Sends prompt requests to the backend API.
- Displays SQL/ETL routing, errors, tables, and downloadable artifacts.
- Selects artifact sources for follow-up transformations.

### `tests/test_sessions.py`

**Role:** Focused coverage for anonymous session behavior.

Tests fixed-TTL session creation, cookie bootstrap, cross-session artifact isolation, and deletion of expired session artifacts.

**Modify this file when:**
- Adjusting the UI or API contract consumption.

### `data/`

**Role:** Seed and generated data storage.

**Important directories/files:**
- `data/*.csv` for the seeded demo tables
- `data/artifacts/` for temp export files
- `data/extract/` and `data/transform/` are historical/default areas for ETL staging, but the active app writes artifact files to `data/artifacts/`

**Do not treat CSVs as the schema source of truth.** The authoritative schema is implemented in `feed_db.py` and enforced by `utils/database.py`.

## Current Architectural Notes

The current implementation is already a public-facing demo with these important properties:

- SQL requests are validated by `validate_select()` and executed in a read-only transaction.
- ETL operations are flat-file only; they do not load into the database.
- Every browser visit receives a fixed 15-minute anonymous session; activity does not extend it.
- Artifacts are session-owned, inaccessible across sessions, and deleted on expiry by the registry sweep.
- URL imports are restricted to safe public HTTP/HTTPS sources.
- The browser-facing API is the stable boundary; internal LangGraph objects are not returned directly.

## Minimal LLM Request Template

Use this when asking for code changes:

```text
Read CODEBASE_FILE_GUIDE.md and README.md first.

Goal: <one concrete behavior>

Relevant request/response example: <example>

Start by reading only the files in the relevant change-location map entry.
Preserve the current public API contract unless the goal explicitly changes it.
Keep the change narrow and verify it with a focused check.
Do not expose secrets or return raw LangChain objects to the browser.
```

## Validation Expectations

After changing source code:
- run a focused Python import/syntax check on the edited files,
- run the relevant tests for the behavior you changed,
- validate the public API path if the change affects `api.py` or the adapter layer,
- confirm that no secrets or credentials are present in the tracked files.
