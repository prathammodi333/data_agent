# Selected-file source priority — v7

The previous version recognized “this file” but missed explicit filenames such as `from query_export.csv`. It also let the word “payment” override the selected CSV. Source selection is now resolved once and used consistently by routing and execution.

## Behavior

| Request and selection | Source |
| --- | --- |
| Filter payment by PayPal and export to CSV; no file selected | Demo database |
| Filter payment method of Apple Pay from query_export.csv; that file selected | Selected CSV |
| Filter payment amount above 100; a file selected | Selected CSV |
| Filter payment amount above 100 from the database; a file selected | Demo database |
| Request names another file, or the selected file expired | Clear error; no database fallback |

An export containing only PayPal rows returns **zero rows** when filtered for Apple Pay. The empty CSV retains its column headers. It does not fetch Apple Pay rows from the database.

The UI now sends `source_mode: "artifact"` with an explicit selection. The API still accepts old requests without that field: a bare artifact ID does not silently redirect a database request. Named file references work with the selected artifact ID; filenames are never treated as server paths or used to search other users' exports.

## Install

1. Stop both services and extract this project into a new folder. Copy the backend and frontend `.env` files into their matching folders.
2. From the project root run `uv sync`, then `uv run uvicorn api:app --reload --port 8000`.
3. From `frontend/` run `npm ci`, then `npm run dev`. Updating the frontend is required so it sends the explicit-selection flag.
4. Refresh the browser and start a new session. Confirm `/api/health` reports `"revision": "artifact-source-priority-7"`.
5. Export PayPal payments, click **Use as transformation source**, then try both follow-up filters in the table above.

Logs include the chosen source kind, selection mode, and source artifact ID. File transformation responses include the source artifact ID and filename under `details`.

## Verification

112 automated tests pass. New API regressions export sample PayPal rows, then filter the selected artifact and assert no further database access, correct empty/nonempty CSV contents, and preserved headers. Tests also cover old artifact IDs, missing/expired sources, filename mismatches, and explicit database overrides. Model calls are mocked; live OpenAI/Neon verification still requires your credentials.
