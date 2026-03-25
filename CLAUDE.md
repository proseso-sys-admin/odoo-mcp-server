# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is an **Odoo MCP Server** — a single Python file (`main.py`) that bridges any Odoo instance to AI assistants via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/). It runs as a Google Cloud Run service and exposes 30+ MCP tools over SSE transport.

## Running Locally

```bash
pip install -r requirements.txt

# Required env vars
export ODOO_CONNECTIONS='{"connections":{},"default":null}'
export MCP_SECRET="your-secret"
export PORT=8080

python main.py
# Server available at http://localhost:8080/sse
# Health check: http://localhost:8080/healthz
```

## Deploying

**Windows (PowerShell):**
```powershell
.\deploy.ps1
```

**CI/CD via Cloud Build:**
```bash
gcloud builds submit . --config cloudbuild.yaml --project odoo-ocr-487104
```

Cloud Run service: `odoo-mcp-server` in region `asia-southeast1`, project `odoo-ocr-487104`.

## Quality Tools

### Smoke test
```bash
./smoke-test.sh http://localhost:8080 $MCP_SECRET
```

### Lint and format (ruff)
```bash
ruff check .
ruff format .
```

### PR checks
`cloudbuild-pr.yaml` runs ruff + syntax check on every pull request. Merges are blocked until checks pass.

### Dependency pinning
Production builds use `requirements.lock` (pinned). Update after changing dependencies:
```bash
pip install -r requirements.txt
pip freeze > requirements.lock
```

## Architecture

The entire server lives in `main.py`. There are no sub-modules.

### Request flow
```
MCP client (SSE) → FastMCP → @mcp.tool() function
                                    ↓
                             _conn(connection)         ← resolves connection string
                                    ↓
                             _execute(conn, ...)       ← XML-RPC or JSON-2 dispatch
                                    ↓
                             Odoo External API
```

### Key internals

| Component | Location | Purpose |
|-----------|----------|---------|
| `_uid_cache` | module-level dict | In-memory UID cache; avoids re-auth per call. Invalidated only on process restart. |
| `_get_connection()` | ~line 68 | Resolves a connection: named key in config, inline JSON string, or `url\|db\|user\|key` pipe-delimited string |
| `_authenticate()` | ~line 101 | Authenticates via XML-RPC `/xmlrpc/2/common`, caches UID |
| `_execute()` | ~line 140 | Dispatches to `_execute_json2()` (Odoo 19+ JSON-2 API) or XML-RPC `execute_kw` |
| `_parse_xmlrpc_error()` | ~line 196 | Converts XML-RPC faults to structured, LLM-readable JSON with actionable hints |
| SSE keep-alive patch | ~line 256 | Monkey-patches `EventSourceResponse.__init__` to inject `ping=15s` — prevents Cloud Run from dropping idle SSE connections |
| `PROTECTED_MODELS` | ~line 47 | Frozenset of models where `odoo_delete` is blocked for safety |
| Context-window caps | ~line 42 | `HARD_CAP_NARROW=1000` (≤5 fields), `HARD_CAP_WIDE=100` (>5 fields) |

### Tool categories (all in `main.py`)

- **Auth/connections** (~line 290): `odoo_list_connections`, `odoo_authenticate`
- **Core CRUD** (~line 323): `odoo_search`, `odoo_read`, `odoo_create`, `odoo_write`, `odoo_delete`, `odoo_copy`, `odoo_count`, `odoo_call`
- **Messaging** (~line 550): `odoo_send_message`
- **Advanced ORM** (~line 598): `odoo_read_group`, `odoo_name_search`, `odoo_name_create`, `odoo_default_get`, `odoo_get_metadata`
- **Schema discovery** (~line 712): `odoo_search_models`, `odoo_get_fields`, `odoo_get_views`, `odoo_get_menus`, `odoo_check_access`, `odoo_list_companies`
- **Batch** (~line 902): `odoo_execute_batch`
- **Files/reports** (~line 931): `odoo_upload_attachment`, `odoo_download_attachment`, `odoo_get_report`
- **Server actions/crons** (~line 1043): `odoo_run_server_action`, `odoo_list_crons`, `odoo_trigger_cron`
- **Custom fields** (~line 1103): `odoo_create_custom_field`
- **Multi-DB extraction** (~line 1174): `odoo_multi_db_extract`
- **AP Worker** (~line 1401): `odoo_trigger_ap_worker`

## Environment Variables

| Variable | Source | Purpose |
|----------|--------|---------|
| `ODOO_CONNECTIONS` | Cloud Run env | JSON blob of named connection configs |
| `MCP_SECRET` | Secret Manager (`odoo-mcp-secret`) | Basic endpoint protection |
| `ODOO_AP_WORKER_URL` | Cloud Run env | URL of the AP Bill OCR worker |
| `ODOO_AP_WORKER_SECRET` | Secret Manager (`odoo-ap-worker-secret`) | Auth for AP worker requests |
| `PORT` | Cloud Run env | HTTP port (default 8080) |

## Odoo Domain Syntax

Odoo uses Polish prefix notation for OR:
```python
['|', ('field', '=', val1), ('field', '=', val2)]  # OR
[('field1', '=', val1), ('field2', '=', val2)]      # AND (implicit)
```

## Adding a New Tool

1. Define a function decorated with `@mcp.tool()` in `main.py`
2. Accept `connection: str` as the first parameter (all tools that talk to Odoo need it)
3. Call `_conn(connection)` to resolve it, then `_execute(conn, model, method, ...)` for ORM calls
4. Return a dict; on error, `_execute` returns `{"error": True, "message": "..."}` — check for it
5. Add a row to the tool table in `CONNECT_INSTRUCTIONS.md`

