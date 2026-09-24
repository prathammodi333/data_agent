> Historical planning document. The implemented public-demo source and README are authoritative; ETL writes flat files only and the public API contract is in `api.py`.

# AI Data Agent: UI Implementation Handoff

## Purpose

This document is the implementation brief for the next LLM or developer who will build and integrate a web interface for this project.

The project is a Python 3.12+ agentic data assistant built with LangChain and LangGraph. A user submits a natural-language request. The main data agent classifies the request and delegates it to one of two specialized agents:

- `sql_analyst`: answers read-only questions about the PostgreSQL database.
- `etl_analyst`: extracts data from APIs or transforms local data files.

The UI should make this feel like one capable data workspace, while preserving the distinction between database analysis and file/API operations.

## Repository Map

```text
main.py                    Current command-line entry point
feed_db.py                 Creates PostgreSQL tables and loads CSV data
agents/data_agent.py       Main router graph and public agent object
agents/sql_analyst.py      SQL generation, safety check, execution, answer graph
agents/etl_analyst.py      ETL tool-calling graph
Models/schema.py           Pydantic state and structured-output models
utils/database.py          PostgreSQL connection, schema inspection, SQL execution
utils/etl_tools.py         API extraction, file loading, Pandas code execution
utils/llm_pick.py          LLM selection by task complexity
data/                      Sample CSV files and ETL output directories
README.md                  Existing project documentation
pyproject.toml             Python dependencies and project metadata
```

## Current Agent Contracts

### Main data agent

The public object is `agents.data_agent.data_agent`, a compiled LangGraph.

Current invocation shape:

```python
from agents.data_agent import data_agent
from langchain_core.messages import HumanMessage

result = data_agent.invoke({
    "messages": [HumanMessage(content="Show the top 5 users by rating")],
    "route_response": "",
})
```

The router reads the latest message, asks an LLM for a structured classification (`sql` or `etl`), then invokes the selected branch. It is synchronous and can take several seconds or longer because it may call multiple LLMs and a database or external API.

`DataAgentSchema` contains:

```python
{
    "messages": list,
    "route_response": str,  # "sql" or "etl"
}
```

The UI integration should initially use one request per user message. Do not expose the internal LangChain message objects directly to a browser. Add a backend adapter that converts the result into a JSON-safe response.

### SQL analyst

The SQL graph runs this flow:

```text
curate_ques
  -> prompt_query_context
  -> generate_sql
  -> is_safe_sql
  -> execute_sql       when is_safe == "Yes"
  -> represent_final_answer
```

Unsafe SQL goes to `canceled_sql` and is not executed.

The final state fields are:

```python
{
    "messages": list,
    "user_question": str,
    "curated_ques": str,
    "prompt_query_context": str,
    "generated_sql_query": str,
    "is_safe": "Yes" | "No",
    "comments": str,
    "sql_query_execution_result": str,
    "final_answer": str,
}
```

For the first UI version, return `final_answer` as the primary response. It is useful to include `route`, `is_safe`, and optionally the generated SQL/result in a collapsed developer-details area, but do not make raw prompts or credentials visible by default.

### ETL analyst

The ETL graph loops between an LLM node and a tool node:

```text
llm_node -> tool_node -> llm_node
llm_node -> end
```

Available tools:

1. `extract_load_tool(url, output_folder, format)`
   - Fetches JSON from an API.
   - Normalizes `data["results"]` with Pandas.
   - Writes `csv`, `json`, or `parquet` output.

2. `transform_load_tool(input_file_path, output_folder, output_format, user_question)`
   - Reads CSV, line-delimited JSON, or Parquet.
   - Gives the LLM the first three rows as context.
   - Generates Pandas code and executes it.
   - Returns a status message, generated code, and execution result.

The ETL branch currently returns a `DataAgentSchema` state whose `messages` list contains the branch response. The UI adapter should extract the final assistant text from the message list and return a stable response object.

## Recommended Backend Boundary

Create a small HTTP API around the existing agent. FastAPI is the preferred choice because it fits the Python project and gives request/response validation and OpenAPI documentation.

Suggested endpoint:

```text
POST /api/chat
```

Request:

```json
{
  "message": "Show the top 5 users by rating"
}
```

Suggested response:

```json
{
  "answer": "...",
  "route": "sql",
  "status": "completed",
  "sql": "...",
  "sql_safe": true,
  "execution_result": "...",
  "artifacts": [],
  "error": null
}
```

For an ETL request, `artifacts` should contain safe, browser-usable metadata such as:

```json
[
  {
    "name": "extracted_data.csv",
    "path": "data/extract/extracted_data.csv",
    "download_url": "/api/files/data/extract/extracted_data.csv",
    "format": "csv"
  }
]
```

Do not return arbitrary filesystem paths as downloadable URLs without validating that the resolved path stays inside an allowed data directory. Add a separate download endpoint with path traversal protection if artifact downloads are implemented.

Suggested additional endpoints:

```text
GET /api/health
GET /api/files/{path}       # only after safe path validation
```

The API must:

- Keep all API keys and database credentials server-side.
- Validate and limit message length.
- Handle agent, database, LLM, timeout, and external API errors as JSON errors.
- Avoid blocking the server event loop; run the synchronous graph in a worker thread or equivalent.
- Never execute SQL supplied directly by the browser. The browser sends a natural-language request only.
- Never trust a client-provided output path.
- Use CORS only for the actual frontend origin in development and deployment.
- Add request timeouts and basic rate limiting before public deployment.

## UI Requirements

Build a responsive data workspace rather than a marketing landing page.

### Primary workflow

1. User enters a natural-language request.
2. UI shows a pending state while the agent runs.
3. UI displays the answer in a readable assistant message.
4. UI labels whether the request was handled by SQL or ETL.
5. UI shows generated files as downloadable artifacts when available.
6. UI provides an expandable technical-details section for SQL, safety status, and execution metadata.

### Suggested layout

- Header: product name, connection/agent health indicator, and a compact reset conversation action.
- Main area: conversation timeline with user and assistant messages.
- Composer: multiline input, submit button, disabled/loading state, and example prompts.
- Result treatment: tables should be rendered as tables when the backend provides structured rows; do not force users to parse Python tuple strings.
- ETL artifact area: file name, format, size if known, and download action.
- Technical details: collapsed by default; include route, SQL safety decision, SQL text, and execution information.

### Example prompts

```text
Show the different payment methods in the database.
What is the average fare by ride status?
Extract https://pokeapi.co/api/v2/pokemon and save it as CSV.
Filter rides.csv to ratings above 4.5 and save the result as JSON.
```

The interface should clearly distinguish an answer from an operation result. For example, an ETL response should state what file was created and provide a download action; an SQL response should prioritize the answer and optionally show tabular data.

## Deployment and Configuration

The application needs:

- Python 3.12 or newer.
- `OPENAI_API_KEY` and/or `ANTHROPIC_API_KEY`, depending on the configured model implementation.
- PostgreSQL/Neon connection values.
- A writable data directory for ETL output.
- A frontend origin configured for API access.

The current SQL code reads these environment variables:

```text
neon_hostname
port
database
user
password
```

The existing `.env.example` uses `host` instead of `neon_hostname`; reconcile this before deployment and never commit real secrets. Rotate any credentials that have ever been committed or shared publicly.

Use environment variables or the hosting provider's secret manager in production. Do not send secrets to the frontend, include them in API responses, or put them in client-side build variables.

## Important Current Limitations to Address During Integration

These are integration concerns, not reasons to rewrite the agents immediately:

- There is currently no web server or stable API response model.
- Agent execution is synchronous.
- SQL execution returns a string representation of rows rather than structured JSON rows.
- ETL extraction assumes the API response has a top-level `results` key.
- ETL transformation executes LLM-generated Python with `exec`; this is unsafe for untrusted inputs and must be sandboxed, restricted, or clearly limited before exposing it publicly.
- Output paths are supplied through tool arguments and need allowlisting/path validation.
- `utils/database.py` performs environment-dependent initialization at import time; the API should avoid importing it during health checks unless configuration is valid.
- LLM model names and provider configuration are currently hard-coded in `utils/llm_pick.py`; deployment should make them configurable and verify that the selected models exist.
- The project contains graph-image generation code that is useful for development but should not run on every web request.

## Implementation Order

1. Add a backend API module without changing the graph behavior.
2. Add Pydantic request and response DTOs and a serializer for SQL and ETL results.
3. Add safe artifact discovery/download handling.
4. Add focused backend tests for SQL responses, unsafe SQL responses, ETL responses, validation errors, and agent exceptions.
5. Build the frontend against the API contract, using mock responses for UI development.
6. Add production configuration, CORS, logging, timeouts, and deployment instructions.
7. Test the full flow with a real database and a controlled ETL fixture before making the demo public.

## Definition of Done

The integration is complete when:

- A user can submit a natural-language request from the browser.
- The browser receives a JSON response without seeing internal LangChain objects.
- SQL questions return a readable answer and never execute an unsafe query.
- ETL requests show operation status and expose only validated output artifacts.
- Loading, empty, validation-error, timeout, agent-error, and unsafe-query states are visible and understandable.
- Secrets remain server-side.
- The backend and frontend can be started with documented commands.
- The deployed project has a health check and a short portfolio-friendly README section explaining the architecture.
