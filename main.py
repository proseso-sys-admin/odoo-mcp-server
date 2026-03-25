#!/usr/bin/env python3
"""
Odoo MCP Server — Cloud Run HTTP edition (v2: Odoo 19 optimized).

Exposes the full Odoo External API (XML-RPC + JSON-2) as MCP tools with:
  - Smart context-window protection (flexible limits, pagination metadata)
  - Full ORM coverage (search, read, create, write, delete, copy, read_group,
    name_search, name_create, default_get, get_metadata)
  - Schema introspection (models, fields, views, menus, access rights)
  - File management (upload / download attachments, generate reports)
  - Server & cron action triggers
  - Batch execution for multi-step tasks
  - Clean error handling with self-correction hints

Environment variables (set via Cloud Run secrets / env):
  ODOO_CONNECTIONS  - JSON string of connections config
  MCP_SECRET        - Secret for basic endpoint protection
  ODOO_AP_WORKER_URL / ODOO_AP_WORKER_SECRET - AP Bill OCR worker
"""

import base64
import json
import os
import re
import time
import xmlrpc.client
from typing import Annotated, Any
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings
from sse_starlette.sse import EventSourceResponse
from starlette.requests import Request
from starlette.responses import PlainTextResponse

# -- Type aliases --------------------------------------------------------------

# Every tool that talks to Odoo requires this parameter.
# The value is the JSON string returned by odoo_authenticate — call that first.
Connection = Annotated[
    str,
    "JSON string returned by odoo_authenticate. "
    "You MUST call odoo_authenticate first and pass its exact return value here.",
]

# -- Constants -----------------------------------------------------------------

DEFAULT_LIMIT = 100
HARD_CAP_NARROW = 1000  # max when fields <= 5
HARD_CAP_WIDE = 100  # max when fields > 5 or unspecified
NARROW_FIELD_THRESHOLD = 5

PROTECTED_MODELS = frozenset(
    {
        "ir.model",
        "ir.module.module",
        "res.company",
        "base",
    }
)

# -- Config --------------------------------------------------------------------

MCP_SECRET = os.environ.get("MCP_SECRET", "")


def load_config() -> dict:
    raw = os.environ.get("ODOO_CONNECTIONS", "")
    if raw:
        return json.loads(raw)
    return {"connections": {}, "default": None}


# -- Odoo XML-RPC helpers ------------------------------------------------------

_uid_cache: dict[str, int] = {}
_field_cache: dict[str, dict] = {}  # key: "url|db|model" → fields_get result
_model_cache: dict[str, list] = {}  # key: "url|db|query|limit" → model list
_model_cache_ts: dict[str, float] = {}  # timestamps for model cache TTL
_MODEL_CACHE_TTL = 3600.0  # 1 hour
_company_cache: dict[str, tuple] = {}  # key: "url|db" → (records, timestamp)
_COMPANY_CACHE_TTL = 600.0  # 10 minutes


def _get_connection(config: dict, key: str) -> dict:
    """Resolve a connection by name, inline JSON, or pipe-delimited string."""
    if not isinstance(key, str):
        raise ValueError(
            f"Connection key must be a string (named key, inline JSON, or url|db|user|key), "
            f"got {type(key).__name__}: {str(key)[:100]!r}"
        )
    conns = config.get("connections", {})

    if key in conns:
        return conns[key]

    # Inline JSON: {"url": "...", "api_key": "...", ...}
    try:
        inline = json.loads(key)
        if isinstance(inline, dict) and "url" in inline and "api_key" in inline:
            if "db" not in inline:
                host = urlparse(inline["url"]).hostname or ""
                inline["db"] = host.split(".")[0]
            if "user" not in inline:
                inline["user"] = "admin"
            return inline
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # Pipe-delimited: url|db|user|api_key
    parts = key.split("|")
    if len(parts) == 4 and parts[0].startswith("http"):
        return {"url": parts[0], "db": parts[1], "user": parts[2], "api_key": parts[3]}
    if len(parts) == 3 and parts[0].startswith("http"):
        host = urlparse(parts[0]).hostname or ""
        return {"url": parts[0], "db": host.split(".")[0], "user": parts[1], "api_key": parts[2]}

    raise ValueError(f"Connection '{key}' not found. Available: {list(conns.keys())}")


def _authenticate(conn: dict) -> tuple[str, int, str]:
    url = conn["url"].rstrip("/")
    db = conn["db"]
    user = conn.get("user", "admin")
    api_key = conn["api_key"]

    cache_key = f"{url}|{db}|{user}"
    if cache_key not in _uid_cache:
        try:
            common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common", allow_none=True)
            uid = common.authenticate(db, user, api_key, {})
        except xmlrpc.client.Fault as e:
            raise ValueError(
                f"Odoo rejected authentication for {user}@{db} on {url}: {str(e.faultString)[:200]}"
            ) from e
        except Exception as e:
            raise ValueError(
                f"Cannot reach Odoo at {url}: {type(e).__name__}: {str(e)[:200]}. "
                "Check that the URL is correct and the server is reachable."
            ) from e
        if not uid:
            raise ValueError(
                f"Authentication failed for {user}@{db} on {url}. Check URL, database name, user login, and API key."
            )
        _uid_cache[cache_key] = uid

    return url, _uid_cache[cache_key], api_key


def _build_context(context: dict | None) -> dict:
    """Merge caller-supplied context with sensible defaults."""
    if not context:
        return {}
    return dict(context)


def _execute(conn: dict, model: str, method: str, *args, **kwargs) -> Any:
    """Execute an Odoo XML-RPC call with structured error handling."""
    transport = conn.get("transport", "xmlrpc")

    if transport == "json2":
        return _execute_json2(conn, model, method, *args, **kwargs)

    url, uid, api_key = _authenticate(conn)
    db = conn["db"]
    obj = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", allow_none=True)
    try:
        return obj.execute_kw(db, uid, api_key, model, method, list(args), kwargs)
    except xmlrpc.client.Fault as e:
        return _parse_xmlrpc_error(e, model, method)


def _execute_json2(conn: dict, model: str, method: str, *args, **kwargs) -> Any:
    """Execute an Odoo JSON-2 API call (/json/2/)."""
    url = conn["url"].rstrip("/")
    db = conn["db"]
    api_key = conn["api_key"]

    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": {
            "model": model,
            "method": method,
            "args": list(args),
            "kwargs": kwargs,
        },
    }

    endpoint = f"{url}/json/2/{db}/dataset/{model}/{method}"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"bearer {api_key}",
    }

    try:
        resp = httpx.post(endpoint, json=payload, headers=headers, timeout=60)
        data = resp.json()
        if "error" in data:
            err = data["error"]
            return {
                "error": True,
                "message": err.get("message", str(err)),
                "debug": err.get("debug", ""),
            }
        return data.get("result")
    except Exception as exc:
        return {"error": True, "message": str(exc)}


def _parse_xmlrpc_error(fault: xmlrpc.client.Fault, model: str, method: str) -> dict:
    """Parse XML-RPC faults into clean, actionable JSON for the LLM."""
    msg = str(fault.faultString)
    result: dict[str, Any] = {"error": True, "raw": msg[:500]}

    field_match = re.search(r"Invalid field '(\w+)' on model '([\w.]+)'", msg)
    if field_match:
        result["message"] = f"Field '{field_match.group(1)}' does not exist on model '{field_match.group(2)}'."
        result["hint"] = "Call odoo_get_fields to discover valid field names for this model."
        return result

    access_match = re.search(r"AccessError", msg)
    if access_match:
        result["message"] = f"Access denied on {model}.{method}."
        result["hint"] = (
            "Call odoo_check_access to verify permissions, or authenticate "
            "with a user that has the required access rights."
        )
        return result

    if "domain" in msg.lower() or "Invalid leaf" in msg:
        result["message"] = f"Invalid domain expression for {model}.{method}."
        result["hint"] = (
            "Odoo uses Polish prefix notation for OR: "
            "['|', ('field','=',val1), ('field','=',val2)]. "
            "AND is implicit between consecutive leaves."
        )
        return result

    validation_match = re.search(r"ValidationError", msg)
    if validation_match:
        result["message"] = f"Validation error on {model}.{method}."
        lines = msg.split("\n")
        for line in lines:
            stripped = line.strip()
            if stripped and "Traceback" not in stripped and "File " not in stripped:
                result["detail"] = stripped
                break
        return result

    result["message"] = f"Odoo error on {model}.{method}: {msg[:300]}"
    return result


# -- SSE keep-alive patch ------------------------------------------------------
# The MCP SDK creates EventSourceResponse without a ping interval, so Cloud Run
# (and similar proxies) kill the idle SSE connection at their request timeout
# boundary. We permanently patch EventSourceResponse to inject ping=15 (seconds)
# and a custom ping message factory that sends an actual event instead of a
# comment, as some SSE client polyfills ignore comments for keep-alive tracking.

from sse_starlette.sse import ServerSentEvent  # noqa: E402

_orig_esr = EventSourceResponse.__init__


def _patched_esr_init(esr_self, *args, **kwargs):
    kwargs.setdefault("ping", 15)
    kwargs.setdefault("ping_message_factory", lambda: ServerSentEvent(event="ping", data="keepalive"))
    _orig_esr(esr_self, *args, **kwargs)


EventSourceResponse.__init__ = _patched_esr_init

# -- MCP Server ----------------------------------------------------------------

_port = int(os.environ.get("PORT", "8080"))
mcp = FastMCP(
    "odoo-connect",
    host="0.0.0.0",
    port=_port,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


# -- Helper to resolve connection shorthand -----------------------------------


def _conn(connection: str) -> dict:
    """Shorthand: resolve a connection key/JSON to a connection dict."""
    config = load_config()
    return _get_connection(config, connection)


# =============================================================================
# AUTHENTICATION & CONNECTIONS
# =============================================================================


@mcp.tool()
def odoo_list_connections() -> dict:
    """List all configured Odoo connections (databases). Call this first to discover available connection keys."""
    config = load_config()
    conns = config.get("connections", {})
    return {
        "connections": {
            k: {"url": v.get("url"), "db": v.get("db"), "user": v.get("user", "admin")} for k, v in conns.items()
        },
        "default": config.get("default"),
    }


@mcp.tool()
def odoo_guide() -> dict:
    """
    Start here. Returns the required workflow and key tool reference for working with Odoo.
    Call this when you are unsure what to do, or at the start of any Odoo session.
    """
    return {
        "required_workflow": [
            "1. AUTHENTICATE — call odoo_authenticate(url, user, api_key) and save the returned connection string",
            "2. DISCOVER MODEL — call odoo_search_models(connection, query) if unsure of the model's technical name (e.g. 'invoice' → 'account.move')",
            "3. DISCOVER FIELDS — call odoo_get_fields(connection, model) before querying; never guess field names",
            "4. QUERY — call odoo_search / odoo_read / odoo_read_group with correct model and field names",
        ],
        "connection_string_rule": (
            "odoo_authenticate returns a JSON string. "
            "Pass that exact string as the 'connection' argument to every other tool."
        ),
        "domain_syntax": {
            "AND (implicit)": "[('field1', '=', val1), ('field2', '=', val2)]",
            "OR": "['|', ('field', '=', val1), ('field', '=', val2)]",
            "NOT": "['!', ('field', '=', val)]",
            "nested OR+AND": "['|', ('a','=',1), '&', ('b','=',2), ('c','=',3)]",
            "all records": "[]",
        },
        "common_mistakes": [
            "Calling odoo_search before odoo_authenticate",
            "Guessing field names — always call odoo_get_fields first",
            "Guessing model names — always call odoo_search_models first",
            "Using Python 'or'/'and' instead of the '|'/'&' prefix operators in domains",
            "Calling odoo_create without odoo_default_get first — auto-filled fields cause validation errors",
            "Calling odoo_read in a loop — pass all IDs at once for bulk read",
            "Trying to set computed fields (e.g. amount_total) — they are read-only and auto-calculated by Odoo",
            "Expecting odoo_execute_batch to be atomic — it is NOT; each operation runs independently",
        ],
        "valid_context_keys": {
            "active_test": "False to include archived/inactive records",
            "lang": "'es_ES' or any locale code to execute in a specific language",
            "allowed_company_ids": "[1, 2] for multi-company context",
            "force_company": "Company ID to force context company",
            "no_recompute": "True to skip expensive recomputations during bulk writes",
        },
        "quick_reference": {
            "find_model": "odoo_search_models(connection, 'invoice')",
            "find_fields": "odoo_get_fields(connection, 'account.move', search_term='amount')",
            "find_writable_fields": "odoo_get_fields — check 'readonly' attribute; skip fields where readonly=True or 'compute' is set",
            "see_ui_fields": "odoo_get_views(connection, 'account.move', 'form') — returns fields_in_view list",
            "aggregate": "odoo_read_group(connection, model, fields=['amount_total:sum'], groupby=['partner_id'])",
            "check_defaults": "odoo_default_get(connection, model, fields) — call BEFORE create for any accounting model",
            "resolve_m2o_id": "odoo_name_search(connection, 'res.partner', 'Acme Corp') — get ID from name",
            "resolve_multiple_m2o": "odoo_name_search_batch(connection, 'res.partner', ['Acme', 'Beta Corp']) — batch lookup",
            "include_archived": "pass context={'active_test': False} to any tool",
            "bulk_read": "odoo_read(connection, model, [id1, id2, id3], fields=[...]) — always batch, never loop",
            "bulk_ops": "odoo_execute_batch(connection, operations=[...]) — non-atomic, check each result",
            "cross_db": "odoo_multi_db_extract(queries=[...]) — runs against all configured DBs at once",
            "ap_ocr": "odoo_trigger_ap_worker(doc_id) — trigger AP Bill OCR worker",
        },
    }


@mcp.tool()
def odoo_authenticate(url: str, user: str, api_key: str, db: str = "", transport: str = "xmlrpc") -> str:
    """
    Validate credentials and return a connection JSON string for all other tools.
    Pass the returned string as the 'connection' argument to every other tool.
    Set transport='json2' to use the Odoo 19+ JSON-2 API instead of XML-RPC.
    """
    if not db:
        host = urlparse(url).hostname or ""
        db = host.split(".")[0]

    conn = {"url": url, "db": db, "user": user, "api_key": api_key, "transport": transport}
    try:
        _authenticate(conn)
    except ValueError as e:
        return json.dumps({"error": True, "message": str(e)})
    return json.dumps(conn)


# =============================================================================
# CORE CRUD — with context-window protection
# =============================================================================


@mcp.tool()
def odoo_search(
    connection: Connection,
    model: str,
    domain: list = [],  # noqa: B006
    fields: list = [],  # noqa: B006
    limit: int = 0,
    offset: int = 0,
    order: str = "",
    context: dict = {},  # noqa: B006
) -> dict:
    """
    Search Odoo records (search_read) with smart pagination.

    BEFORE CALLING: use odoo_search_models to confirm the model's technical name,
    and odoo_get_fields to discover valid field names. Never guess either.

    Domain syntax — AND is implicit; OR uses the '|' prefix operator:
      AND: [('field1', '=', val1), ('field2', '=', val2)]
      OR:  ['|', ('field', '=', val1), ('field', '=', val2)]

    Returns {"records": [...], "metadata": {"count", "total", "has_more", "next_offset"}}.
    Default fields are ['id', 'display_name'] if omitted.

    FIELD CAP RULE: limit is auto-capped based on field count:
      - <= 5 fields: cap = 1000
      - >  5 fields: cap =  100  ← REQUESTING MANY FIELDS DRASTICALLY REDUCES LIMIT
    Use odoo_read_group for aggregations; request only the fields you need here.
    Use metadata.has_more + next_offset to paginate.
    """
    conn = _conn(connection)

    if not fields:
        fields = ["id", "display_name"]

    effective_limit = limit if limit > 0 else DEFAULT_LIMIT
    cap = HARD_CAP_NARROW if len(fields) <= NARROW_FIELD_THRESHOLD else HARD_CAP_WIDE
    capped = False
    if effective_limit > cap:
        effective_limit = cap
        capped = True

    kw: dict[str, Any] = {
        "limit": effective_limit,
        "offset": offset,
        "fields": fields,
    }
    if order:
        kw["order"] = order
    ctx = _build_context(context)
    if ctx:
        kw["context"] = ctx

    records = _execute(conn, model, "search_read", domain, **kw)

    if isinstance(records, dict) and records.get("error"):
        return records

    total_kw: dict[str, Any] = {}
    if ctx:
        total_kw["context"] = ctx
    total = _execute(conn, model, "search_count", domain, **total_kw)
    if isinstance(total, dict) and total.get("error"):
        total = -1

    meta: dict[str, Any] = {
        "count": len(records),
        "total": total,
        "offset": offset,
        "limit": effective_limit,
        "has_more": (offset + len(records)) < total if isinstance(total, int) else False,
        "next_offset": offset + len(records),
    }
    if capped:
        meta["warning"] = f"Limit capped to {cap}. Request <=5 fields to allow up to {HARD_CAP_NARROW}."

    return {"records": records, "metadata": meta}


@mcp.tool()
def odoo_read(
    connection: Connection,
    model: str,
    ids: list,
    fields: list = [],  # noqa: B006
    context: dict = {},  # noqa: B006
) -> list:
    """
    Read MULTIPLE Odoo records by ID in a single call. Pass a list of IDs.
    IMPORTANT: Always pass all IDs at once — never loop and call this per record.
    Defaults to ['id', 'display_name'] if fields omitted.
    """
    conn = _conn(connection)
    if not fields:
        fields = ["id", "display_name"]
    kw: dict[str, Any] = {"fields": fields}
    ctx = _build_context(context)
    if ctx:
        kw["context"] = ctx
    return _execute(conn, model, "read", ids, **kw)


@mcp.tool()
def odoo_create(
    connection: Connection,
    model: str,
    vals: dict,
    context: dict = {},  # noqa: B006
) -> dict:
    """
    Create a new Odoo record. Returns {"id": <new_id>}.

    CRITICAL — call odoo_default_get FIRST to see what Odoo auto-fills.
    Many models have sequences, default journals, or computed fields that
    silently override values you supply. Skipping this causes validation
    errors on accounting models (account.move, account.payment, etc.).
    Use odoo_create_guided to have defaults applied automatically.

    BEFORE CALLING: use odoo_get_fields to know required fields.
    """
    conn = _conn(connection)
    kw: dict[str, Any] = {}
    ctx = _build_context(context)
    if ctx:
        kw["context"] = ctx
    new_id = _execute(conn, model, "create", vals, **kw)
    if isinstance(new_id, dict) and new_id.get("error"):
        return new_id
    return {"id": new_id}


@mcp.tool()
def odoo_create_guided(
    connection: Connection,
    model: str,
    vals: dict,
    context: dict = {},  # noqa: B006
) -> dict:
    """
    Create a new Odoo record with automatic defaults applied — safer than odoo_create.

    Workflow (all in one call):
      1. Fetch defaults via default_get for the fields you're supplying
      2. Merge: your vals override defaults (you stay in control)
      3. Create the record

    Returns {"id": <new_id>, "defaults_applied": {...}, "final_vals": {...}}
    so you can see exactly what was sent to Odoo.

    Use this for accounting models (account.move, account.payment) where
    missing defaults cause validation errors. For simple models, odoo_create is fine.
    """
    conn = _conn(connection)
    ctx = _build_context(context)

    # Step 1: fetch defaults for the fields being supplied
    default_kw: dict[str, Any] = {}
    if ctx:
        default_kw["context"] = ctx
    defaults = _execute(conn, model, "default_get", list(vals.keys()), **default_kw)
    if isinstance(defaults, dict) and defaults.get("error"):
        defaults = {}

    # Step 2: merge — caller values win
    final_vals = {**defaults, **vals}

    # Step 3: create
    create_kw: dict[str, Any] = {}
    if ctx:
        create_kw["context"] = ctx
    new_id = _execute(conn, model, "create", final_vals, **create_kw)
    if isinstance(new_id, dict) and new_id.get("error"):
        return new_id

    return {
        "id": new_id,
        "defaults_applied": {k: v for k, v in defaults.items() if k not in vals},
        "final_vals": final_vals,
    }


@mcp.tool()
def odoo_write(
    connection: Connection,
    model: str,
    ids: list,
    vals: dict,
    context: dict = {},  # noqa: B006
) -> dict:
    """
    Update existing Odoo records. Returns {"success": True, "updated_ids": [...]}.

    BEFORE CALLING: use odoo_get_fields to verify field names are correct.
    Use odoo_search first to find the record IDs to update.
    """
    conn = _conn(connection)
    kw: dict[str, Any] = {}
    ctx = _build_context(context)
    if ctx:
        kw["context"] = ctx
    result = _execute(conn, model, "write", ids, vals, **kw)
    if isinstance(result, dict) and result.get("error"):
        return result
    return {"success": result, "updated_ids": ids}


@mcp.tool()
def odoo_delete(
    connection: Connection,
    model: str,
    ids: list,
    context: dict = {},  # noqa: B006
) -> dict:
    """
    Delete Odoo records (unlink). Refuses to delete records from protected system models.
    Returns confirmation with count of deleted records.
    """
    if model in PROTECTED_MODELS:
        return {
            "error": True,
            "message": f"Deletion from '{model}' is blocked for safety. Use odoo_call if you truly need this.",
        }
    conn = _conn(connection)
    kw: dict[str, Any] = {}
    ctx = _build_context(context)
    if ctx:
        kw["context"] = ctx
    result = _execute(conn, model, "unlink", ids, **kw)
    if isinstance(result, dict) and result.get("error"):
        return result
    return {"success": result, "deleted_ids": ids, "count": len(ids)}


@mcp.tool()
def odoo_copy(
    connection: Connection,
    model: str,
    id: int,
    default: dict = {},  # noqa: B006
    context: dict = {},  # noqa: B006
) -> dict:
    """
    Duplicate an Odoo record using copy(). Pass 'default' dict to override fields
    on the copy (e.g., {"name": "Copy of ..."}).
    """
    conn = _conn(connection)
    kw: dict[str, Any] = {}
    ctx = _build_context(context)
    if ctx:
        kw["context"] = ctx
    if default:
        kw["default"] = default
    new_id = _execute(conn, model, "copy", [id], **kw)
    if isinstance(new_id, dict) and new_id.get("error"):
        return new_id
    return {"id": new_id}


@mcp.tool()
def odoo_count(
    connection: Connection,
    model: str,
    domain: list = [],  # noqa: B006
    context: dict = {},  # noqa: B006
) -> dict:
    """
    Count records matching a domain filter.
    Domain syntax — AND implicit; OR uses prefix '|':
      AND: [('field1', '=', val1), ('field2', '=', val2)]
      OR:  ['|', ('field', '=', val1), ('field', '=', val2)]
    """
    conn = _conn(connection)
    kw: dict[str, Any] = {}
    ctx = _build_context(context)
    if ctx:
        kw["context"] = ctx
    count = _execute(conn, model, "search_count", domain, **kw)
    if isinstance(count, dict) and count.get("error"):
        return count
    return {"count": count}


@mcp.tool()
def odoo_call(
    connection: Connection,
    model: str,
    method: str,
    args: list = [],  # noqa: B006
    kwargs: dict = {},  # noqa: B006
    context: dict = {},  # noqa: B006
) -> dict:
    """
    Call any Odoo method directly (execute_kw).
    Use for action_post, action_register_payment, or any method not covered by other tools.
    args: positional arguments — for instance methods, the first arg is typically a list of record IDs.
    kwargs: keyword arguments passed directly to execute_kw.
    """
    conn = _conn(connection)
    kw = dict(kwargs)
    ctx = _build_context(context)
    if ctx:
        existing = kw.get("context", {})
        existing.update(ctx)
        kw["context"] = existing
    result = _execute(conn, model, method, *args, **kw)
    return {"result": result}


# =============================================================================
# MESSAGING & CHATTER
# =============================================================================


@mcp.tool()
def odoo_send_message(
    connection: Connection,
    model: str,
    res_id: int,
    body: str,
    subject: str = "",
    message_type: str = "comment",
    subtype_xmlid: str = "mail.mt_comment",
    partner_ids: list = [],  # noqa: B006
    context: dict = {},  # noqa: B006
) -> dict:
    """
    Post a message or send an email via Odoo's Chatter on a specific record.
    model: e.g., 'project.task', 'account.move', 'crm.lead'.
    res_id: the ID of the record.
    body: the HTML body of the message.
    message_type: 'comment' (customer visible) or 'notification' (internal note).
    partner_ids: list of partner IDs to notify/email.
    """
    conn = _conn(connection)
    kw: dict[str, Any] = {
        "body": body,
        "message_type": message_type,
        "subtype_xmlid": subtype_xmlid,
    }
    if subject:
        kw["subject"] = subject
    if partner_ids:
        kw["partner_ids"] = partner_ids

    ctx = _build_context(context)
    if ctx:
        kw["context"] = ctx

    result = _execute(conn, model, "message_post", [res_id], **kw)
    if isinstance(result, dict) and result.get("error"):
        return result
    return {"message_id": result, "success": True}


# =============================================================================
# ADVANCED ORM TOOLS
# =============================================================================


@mcp.tool()
def odoo_read_group(
    connection: Connection,
    model: str,
    domain: list = [],  # noqa: B006
    fields: list = [],  # noqa: B006
    groupby: list = [],  # noqa: B006
    orderby: str = "",
    limit: int = 0,
    lazy: bool = True,
    context: dict = {},  # noqa: B006
) -> dict:
    """
    Aggregate records using read_group (like SQL GROUP BY).
    Prefer this over odoo_search when you need totals, counts, or grouped data.

    fields: aggregation specs — valid functions: sum, avg, min, max, count, count_distinct, array_agg
      e.g. ['amount_total:sum', 'id:count', 'amount_residual:avg']
    groupby: group dimensions, e.g. ['partner_id', 'date:month', 'state']

    Domain syntax — AND implicit; OR uses prefix '|':
      AND: [('field1', '=', val1), ('field2', '=', val2)]
      OR:  ['|', ('field', '=', val1), ('field', '=', val2)]

    Returns {"groups": [...], "count": N} — each group has __count and aggregated values.
    """
    conn = _conn(connection)
    kw: dict[str, Any] = {"lazy": lazy}
    if orderby:
        kw["orderby"] = orderby
    if limit > 0:
        kw["limit"] = limit
    ctx = _build_context(context)
    if ctx:
        kw["context"] = ctx
    result = _execute(conn, model, "read_group", domain, fields, groupby, **kw)
    if isinstance(result, dict) and result.get("error"):
        return result
    return {"groups": result, "count": len(result) if isinstance(result, list) else 0}


@mcp.tool()
def odoo_name_search(
    connection: Connection,
    model: str,
    name: str = "",
    domain: list = [],  # noqa: B006
    operator: str = "ilike",
    limit: int = 10,
    context: dict = {},  # noqa: B006
) -> list:
    """
    Fuzzy search by display name (like Odoo's Many2one dropdown).
    Returns [(id, display_name), ...]. Much faster than search_read for ID lookups.

    Use this to resolve human names to IDs before create/write:
      partner ID  → odoo_name_search(conn, 'res.partner', 'Acme Corp')
      product ID  → odoo_name_search(conn, 'product.product', 'Widget A')
      journal ID  → odoo_name_search(conn, 'account.journal', 'Bank')

    domain: extra filter (AND with name match), e.g. [('customer_rank', '>', 0)]
    operator: 'ilike' (default, case-insensitive contains), '=ilike', '='
    """
    conn = _conn(connection)
    kw: dict[str, Any] = {
        "name": name,
        "args": domain,
        "operator": operator,
        "limit": limit,
    }
    ctx = _build_context(context)
    if ctx:
        kw["context"] = ctx
    return _execute(conn, model, "name_search", **kw)


@mcp.tool()
def odoo_name_search_batch(
    connection: Connection,
    model: str,
    names: list,
    operator: str = "ilike",
    limit_per_name: int = 5,
    context: dict = {},  # noqa: B006
) -> dict:
    """
    Resolve multiple names to (id, display_name) pairs in one tool call.
    Use instead of calling odoo_name_search repeatedly in a loop.

    names: list of search terms, e.g. ['Acme Corp', 'Beta Ltd', 'Gamma Inc']
    Returns {"results": {"Acme Corp": [(id, name), ...], ...}, "not_found": [...]}

    Typical use: resolve partner names, product codes, or account names to IDs
    before calling odoo_create or odoo_write with many2one fields.
    """
    conn = _conn(connection)
    kw_base: dict[str, Any] = {
        "args": [],
        "operator": operator,
        "limit": limit_per_name,
    }
    ctx = _build_context(context)
    if ctx:
        kw_base["context"] = ctx

    results: dict[str, Any] = {}
    not_found: list[str] = []

    for name in names:
        kw = {**kw_base, "name": name}
        matches = _execute(conn, model, "name_search", **kw)
        if (isinstance(matches, dict) and matches.get("error")) or matches:
            results[name] = matches
        else:
            results[name] = []
            not_found.append(name)

    return {"results": results, "not_found": not_found}


@mcp.tool()
def odoo_name_create(
    connection: Connection,
    model: str,
    name: str,
    context: dict = {},  # noqa: B006
) -> dict:
    """
    Quick-create a record by display name only (like Many2one quick-create).
    Odoo fills all defaults. Returns (id, display_name).
    """
    conn = _conn(connection)
    kw: dict[str, Any] = {}
    ctx = _build_context(context)
    if ctx:
        kw["context"] = ctx
    result = _execute(conn, model, "name_create", name, **kw)
    if isinstance(result, dict) and result.get("error"):
        return result
    return {"id": result[0], "display_name": result[1]}


@mcp.tool()
def odoo_default_get(
    connection: Connection,
    model: str,
    fields: list = [],  # noqa: B006
    context: dict = {},  # noqa: B006
) -> dict:
    """
    Get default values Odoo would pre-fill for a new record.
    Call before create() to understand auto-filled fields (sequences, journals, etc.).
    """
    conn = _conn(connection)
    kw: dict[str, Any] = {}
    ctx = _build_context(context)
    if ctx:
        kw["context"] = ctx
    return _execute(conn, model, "default_get", fields, **kw)


@mcp.tool()
def odoo_get_metadata(
    connection: Connection,
    model: str,
    ids: list,
) -> list:
    """
    Get audit metadata for records: create_uid, create_date, write_uid, write_date, xmlid.
    """
    conn = _conn(connection)
    return _execute(conn, model, "get_metadata", ids)


# =============================================================================
# SCHEMA DISCOVERY & INTROSPECTION
# =============================================================================


@mcp.tool()
def odoo_search_models(
    connection: Connection,
    query: str = "",
    limit: int = 20,
) -> dict:
    """
    Search for Odoo models by name or description.
    Example: query='invoice' finds account.move. Returns technical name, label, and info.
    If has_more=True, increase limit or refine query to see remaining results.
    """
    conn = _conn(connection)
    cache_key = f"{conn['url']}|{conn['db']}|{query}|{limit}"
    now = time.time()
    if cache_key in _model_cache and (now - _model_cache_ts.get(cache_key, 0)) < _MODEL_CACHE_TTL:
        cached = _model_cache[cache_key]
        has_more = len(cached) > limit
        return {
            "models": cached[:limit],
            "count": min(len(cached), limit),
            "has_more": has_more,
            "hint": "Increase limit or refine query to see more." if has_more else "",
            "cached": True,
        }

    domain: list = []
    if query:
        domain = [
            "|",
            "|",
            ("model", "ilike", query),
            ("name", "ilike", query),
            ("info", "ilike", query),
        ]
    models = _execute(
        conn,
        "ir.model",
        "search_read",
        domain,
        fields=["model", "name", "info", "state", "transient"],
        limit=limit + 1,
    )
    if isinstance(models, dict) and models.get("error"):
        return models
    _model_cache[cache_key] = models
    _model_cache_ts[cache_key] = now
    has_more = len(models) > limit
    return {
        "models": models[:limit],
        "count": min(len(models), limit),
        "has_more": has_more,
        "hint": "Increase limit or refine query to see more." if has_more else "",
    }


@mcp.tool()
def odoo_get_fields(
    connection: Connection,
    model: str,
    attributes: list = ["string", "type", "required", "relation", "help"],  # noqa: B006
    search_term: str = "",
    field_type: str = "",
) -> dict:
    """
    Get field definitions for an Odoo model.
    Optionally filter by search_term (matches field name or label) or
    field_type (e.g., 'many2one', 'char', 'monetary', 'one2many').
    """
    conn = _conn(connection)
    cache_key = f"{conn['url']}|{conn['db']}|{model}"
    all_fields = _field_cache.get(cache_key)

    if all_fields is None:
        all_fields = _execute(conn, model, "fields_get", [], attributes=attributes)
        if isinstance(all_fields, dict) and all_fields.get("error"):
            return all_fields
        _field_cache[cache_key] = all_fields

    if search_term or field_type:
        filtered = {}
        st = search_term.lower()
        ft = field_type.lower()
        for fname, fdef in all_fields.items():
            if st and st not in fname.lower() and st not in fdef.get("string", "").lower():
                continue
            if ft and fdef.get("type", "").lower() != ft:
                continue
            filtered[fname] = fdef
        return filtered

    return all_fields


def _enrich_view_result(arch: str, view_id: Any, model: str, view_type: str) -> dict:
    """Extract field metadata from view XML arch string."""
    fields_in_view: list[str] = []
    required_fields: list[str] = []
    readonly_fields: list[str] = []
    if arch:
        for match in re.finditer(r"<field\s+([^>]+)>", arch):
            attrs_str = match.group(1)
            name_m = re.search(r'name=["\'](\w+)["\']', attrs_str)
            if not name_m:
                continue
            fname = name_m.group(1)
            if fname not in fields_in_view:
                fields_in_view.append(fname)
            if re.search(r'required=["\']1["\']', attrs_str):
                required_fields.append(fname)
            if re.search(r'readonly=["\']1["\']', attrs_str):
                readonly_fields.append(fname)
    return {
        "arch": arch,
        "view_id": view_id,
        "model": model,
        "type": view_type,
        "fields_in_view": fields_in_view,
        "required_fields": required_fields,
        "readonly_fields": readonly_fields,
    }


@mcp.tool()
def odoo_get_views(
    connection: Connection,
    model: str,
    view_type: str = "form",
    context: dict = {},  # noqa: B006
) -> dict:
    """
    Get the XML architecture of an Odoo view (form, list, search, kanban).
    Returns both raw arch XML and a structured summary of fields visible in the UI:
      - fields_in_view: all field names appearing in the view
      - required_fields: fields marked required="1" in the view
      - readonly_fields: fields marked readonly="1" in the view
    Use fields_in_view to know which fields matter to the user for this view.
    """
    conn = _conn(connection)
    kw: dict[str, Any] = {}
    ctx = _build_context(context)
    if ctx:
        kw["context"] = ctx
    try:
        result = _execute(
            conn,
            model,
            "get_views",
            [[False, view_type]],
            **kw,
        )
        if isinstance(result, dict) and not result.get("error"):
            views = result.get("views", {})
            view_data = views.get(view_type, {})
            arch = view_data.get("arch", "")
            return _enrich_view_result(arch, view_data.get("id"), model, view_type)
        return result
    except Exception:
        result = _execute(
            conn,
            model,
            "fields_view_get",
            view_type=view_type,
        )
        if isinstance(result, dict) and not result.get("error"):
            arch = result.get("arch", "")
            return _enrich_view_result(arch, result.get("view_id"), model, view_type)
        return result


@mcp.tool()
def odoo_get_menus(
    connection: Connection,
    model: str = "",
    limit: int = 50,
) -> list:
    """
    Query Odoo menu structure (ir.ui.menu). Optionally filter by the model
    the menu action points to. Useful for understanding where a model lives in the UI.
    """
    conn = _conn(connection)
    if model:
        actions = _execute(
            conn,
            "ir.actions.act_window",
            "search_read",
            [("res_model", "=", model)],
            fields=["id", "name", "res_model", "view_mode", "domain", "context"],
            limit=limit,
        )
        if isinstance(actions, dict) and actions.get("error"):
            return actions

        action_ids = [f"ir.actions.act_window,{a['id']}" for a in actions]
        if not action_ids:
            return {"message": f"No menu actions found for model '{model}'."}

        menus = _execute(
            conn,
            "ir.ui.menu",
            "search_read",
            [("action", "in", action_ids)],
            fields=["id", "name", "complete_name", "action", "parent_id"],
            limit=limit,
        )
        return {"actions": actions, "menus": menus}

    return _execute(
        conn,
        "ir.ui.menu",
        "search_read",
        [],
        fields=["id", "name", "complete_name", "parent_id", "action"],
        limit=limit,
    )


@mcp.tool()
def odoo_check_access(
    connection: Connection,
    model: str,
) -> dict:
    """
    Check current user's access rights on a model (read/write/create/unlink).
    Queries ir.model.access for the user's groups.
    Call this before attempting writes/deletes on unfamiliar models to avoid
    unexpected AccessError failures at execution time.
    """
    conn = _conn(connection)

    access_records = _execute(
        conn,
        "ir.model.access",
        "search_read",
        [("model_id.model", "=", model)],
        fields=["name", "group_id", "perm_read", "perm_write", "perm_create", "perm_unlink"],
        limit=50,
    )
    if isinstance(access_records, dict) and access_records.get("error"):
        return access_records

    return {
        "model": model,
        "access_rules": access_records,
        "hint": "Rules with group_id=False apply to all users. Others apply only to members of that group.",
    }


@mcp.tool()
def odoo_list_companies(connection: Connection) -> list:
    """List all companies in an Odoo database (for multi-company setups)."""
    conn = _conn(connection)
    cache_key = f"{conn['url']}|{conn['db']}"
    now = time.time()
    cached_entry = _company_cache.get(cache_key)
    if cached_entry and (now - cached_entry[1]) < _COMPANY_CACHE_TTL:
        return cached_entry[0]
    result = _execute(
        conn,
        "res.company",
        "search_read",
        [],
        fields=["id", "name", "currency_id", "country_id"],
    )
    if not (isinstance(result, dict) and result.get("error")):
        _company_cache[cache_key] = (result, now)
    return result


# =============================================================================
# BATCH EXECUTION
# =============================================================================


@mcp.tool()
def odoo_execute_batch(
    connection: Connection,
    operations: list,
) -> dict:
    """
    Execute multiple Odoo operations in a single tool call.
    Each operation: {"model": "...", "method": "...", "args": [...], "kwargs": {...}}

    IMPORTANT: Operations are NOT atomic. If operation [2] fails, operations [0,1,3,4...]
    still execute — there is no rollback. Check results[i] for {"error": True} on each item.
    For atomic multi-step workflows, use a server action instead (odoo_run_server_action).

    Returns {"results": [...], "count": N} — results are in the same order as operations.
    """
    conn = _conn(connection)
    results = []
    for i, op in enumerate(operations):
        model = op.get("model", "")
        method = op.get("method", "")
        args = op.get("args", [])
        kwargs = op.get("kwargs", {})
        if not model or not method:
            results.append({"error": True, "message": f"Operation {i}: missing model or method."})
            continue
        result = _execute(conn, model, method, *args, **kwargs)
        results.append(result)
    return {"results": results, "count": len(results)}


# =============================================================================
# FILE & ATTACHMENT MANAGEMENT
# =============================================================================


@mcp.tool()
def odoo_upload_attachment(
    connection: Connection,
    name: str,
    data_base64: str,
    res_model: str = "",
    res_id: int = 0,
    context: dict = {},  # noqa: B006
) -> dict:
    """
    Upload a file to Odoo as an ir.attachment.
    data_base64: the file content encoded as base64.
    Optionally attach to a record by setting res_model and res_id.
    Maximum file size: 25 MB (Odoo's default upload limit).
    """
    conn = _conn(connection)

    # Validate base64 and enforce size limit
    try:
        raw_bytes = base64.b64decode(data_base64, validate=True)
    except Exception:
        return {"error": True, "message": "data_base64 is not valid base64-encoded data."}
    max_bytes = 25 * 1024 * 1024  # 25 MB
    if len(raw_bytes) > max_bytes:
        size_mb = len(raw_bytes) / (1024 * 1024)
        return {
            "error": True,
            "message": f"File too large: {size_mb:.1f} MB. Odoo's default limit is 25 MB.",
        }

    vals: dict[str, Any] = {
        "name": name,
        "datas": data_base64,
        "type": "binary",
    }
    if res_model:
        vals["res_model"] = res_model
    if res_id:
        vals["res_id"] = res_id

    kw: dict[str, Any] = {}
    ctx = _build_context(context)
    if ctx:
        kw["context"] = ctx

    att_id = _execute(conn, "ir.attachment", "create", vals, **kw)
    if isinstance(att_id, dict) and att_id.get("error"):
        return att_id
    return {"id": att_id, "name": name}


@mcp.tool()
def odoo_download_attachment(
    connection: Connection,
    attachment_id: int,
) -> dict:
    """
    Download an attachment from Odoo. Returns base64 data and filename.
    """
    conn = _conn(connection)
    records = _execute(
        conn,
        "ir.attachment",
        "read",
        [attachment_id],
        fields=["name", "datas", "mimetype", "file_size"],
    )
    if isinstance(records, dict) and records.get("error"):
        return records
    if not records:
        return {"error": True, "message": f"Attachment {attachment_id} not found."}
    rec = records[0]
    return {
        "id": attachment_id,
        "name": rec.get("name"),
        "mimetype": rec.get("mimetype"),
        "file_size": rec.get("file_size"),
        "data_base64": rec.get("datas", ""),
    }


# =============================================================================
# REPORT GENERATION
# =============================================================================


@mcp.tool()
def odoo_get_report(
    connection: Connection,
    report_name: str,
    record_ids: list,
    context: dict = {},  # noqa: B006
) -> dict:
    """
    Generate an Odoo PDF report (invoice, delivery slip, etc.).
    report_name: the technical report name (e.g., 'account.report_invoice').
    Returns base64-encoded PDF data.
    """
    conn = _conn(connection)
    url, uid, api_key = _authenticate(conn)
    db = conn["db"]

    report_obj = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/report", allow_none=True)
    try:
        kw: dict[str, Any] = {}
        ctx = _build_context(context)
        if ctx:
            kw["context"] = ctx
        result = report_obj.render_report(db, uid, api_key, report_name, record_ids, kw)
        if isinstance(result, dict) and result.get("result"):
            return {
                "report_name": report_name,
                "record_ids": record_ids,
                "format": "pdf",
                "data_base64": result["result"],
            }
        return {"report_name": report_name, "result": result}
    except xmlrpc.client.Fault as e:
        return _parse_xmlrpc_error(e, "ir.actions.report", "render_report")
    except Exception as exc:
        return {"error": True, "message": str(exc)}


# =============================================================================
# SERVER ACTIONS & CRONS
# =============================================================================


@mcp.tool()
def odoo_run_server_action(
    connection: Connection,
    action_id: int,
    context: dict = {},  # noqa: B006
) -> dict:
    """
    Execute an ir.actions.server record (custom business logic configured in Odoo).
    """
    conn = _conn(connection)
    kw: dict[str, Any] = {}
    ctx = _build_context(context)
    if ctx:
        kw["context"] = ctx
    result = _execute(conn, "ir.actions.server", "run", [action_id], **kw)
    if isinstance(result, dict) and result.get("error"):
        return result
    return {"action_id": action_id, "result": result}


@mcp.tool()
def odoo_list_crons(
    connection: Connection,
    model: str = "",
    active_only: bool = True,
) -> list:
    """
    List scheduled actions (ir.cron). Optionally filter by model.
    """
    conn = _conn(connection)
    domain: list = []
    if model:
        domain.append(("model_id.model", "=", model))
    if active_only:
        domain.append(("active", "=", True))
    return _execute(
        conn,
        "ir.cron",
        "search_read",
        domain,
        fields=["id", "name", "model_id", "interval_number", "interval_type", "nextcall", "active", "numbercall"],
        limit=100,
    )


@mcp.tool()
def odoo_trigger_cron(
    connection: Connection,
    cron_id: int,
) -> dict:
    """Manually trigger a specific ir.cron scheduled action to run immediately."""
    conn = _conn(connection)
    result = _execute(conn, "ir.cron", "method_direct_trigger", [cron_id])
    if isinstance(result, dict) and result.get("error"):
        return result
    return {"cron_id": cron_id, "triggered": True, "result": result}


# =============================================================================
# CUSTOM FIELD CREATION (Studio-less)
# =============================================================================


@mcp.tool()
def odoo_create_custom_field(
    connection: Connection,
    model: str,
    name: str,
    field_type: str,
    string: str = "",
    required: bool = False,
    help: str = "",
    selection: list = [],  # noqa: B006
    relation: str = "",
    relation_field: str = "",
    domain: str = "",
    context: dict = {},  # noqa: B006
) -> dict:
    """
    Create a custom x_ field on an Odoo model via ir.model.fields (no Studio needed).
    name: must start with 'x_' (auto-prefixed if not).
    field_type: char, text, integer, float, boolean, date, datetime, many2one, one2many, many2many, selection, html, binary.
    """
    if not name.startswith("x_"):
        name = f"x_{name}"

    conn = _conn(connection)

    model_records = _execute(
        conn,
        "ir.model",
        "search_read",
        [("model", "=", model)],
        fields=["id"],
        limit=1,
    )
    if isinstance(model_records, dict) and model_records.get("error"):
        return model_records
    if not model_records:
        return {"error": True, "message": f"Model '{model}' not found."}

    vals: dict[str, Any] = {
        "model_id": model_records[0]["id"],
        "name": name,
        "ttype": field_type,
        "field_description": string or name.replace("x_", "").replace("_", " ").title(),
        "required": required,
    }
    if help:
        vals["help"] = help
    if selection and field_type == "selection":
        vals["selection_ids"] = [(0, 0, {"value": s[0], "name": s[1]}) for s in selection]
    if relation:
        vals["relation"] = relation
    if relation_field:
        vals["relation_field"] = relation_field
    if domain:
        vals["domain"] = domain

    kw: dict[str, Any] = {}
    ctx = _build_context(context)
    if ctx:
        kw["context"] = ctx

    field_id = _execute(conn, "ir.model.fields", "create", vals, **kw)
    if isinstance(field_id, dict) and field_id.get("error"):
        return field_id
    return {"id": field_id, "name": name, "model": model, "type": field_type}


# =============================================================================
# MULTI-DATABASE EXTRACTION
# =============================================================================


@mcp.tool()
def odoo_multi_db_extract(
    queries: list,
    connections: list = [],  # noqa: B006
    stop_on_error: bool = False,
) -> dict:
    """
    Extract information from multiple Odoo databases in one call.

    Runs every query against every connection (or all configured connections
    if 'connections' is empty), then returns results grouped by connection.

    queries: list of query dicts, each with:
      - label     (str, required): friendly name for this query (e.g. "open_invoices")
      - model     (str, required): Odoo model (e.g. "account.move")
      - method    (str, optional): ORM method, default "search_read"
      - domain    (list, optional): Odoo domain filter, default []
      - fields    (list, optional): fields to return, default ["id","display_name"]
      - limit     (int, optional): max records, default 100
      - offset    (int, optional): pagination offset, default 0
      - order     (str, optional): sort order
      - groupby   (list, optional): for read_group queries
      - kwargs    (dict, optional): extra kwargs passed to execute_kw
      - context   (dict, optional): Odoo context overrides

    connections: list of connection keys to query. If empty, ALL configured
                 connections are used.

    stop_on_error: if True, abort remaining connections on first error.

    Example:
      queries=[
        {"label": "open_invoices", "model": "account.move",
         "domain": [["state","=","posted"],["payment_state","!=","paid"],["move_type","in",["out_invoice","out_refund"]]],
         "fields": ["name","partner_id","amount_total","amount_residual","invoice_date_due","state"]},
        {"label": "customer_count", "model": "res.partner",
         "method": "search_count", "domain": [["customer_rank",">",0]]},
        {"label": "revenue_by_month", "model": "account.move.line",
         "method": "read_group",
         "domain": [["parent_state","=","posted"],["account_id.account_type","=","income"]],
         "fields": ["balance:sum"], "groupby": ["date:month"]}
      ]

    Returns:
      {
        "results": {
          "connection_key": {
            "url": "...", "db": "...",
            "queries": {
              "open_invoices": {"records": [...], "metadata": {...}},
              "customer_count": {"count": 42},
              ...
            },
            "errors": []
          },
          ...
        },
        "summary": {"total_connections": 3, "successful": 3, "failed": 0}
      }
    """
    config = load_config()
    all_conns = config.get("connections", {})

    target_keys = connections if connections else list(all_conns.keys())
    if not target_keys:
        return {
            "error": True,
            "message": "No connections configured. Set ODOO_CONNECTIONS or pass explicit connection keys.",
        }

    if not queries:
        return {"error": True, "message": "No queries provided. Pass at least one query dict with 'label' and 'model'."}

    for i, q in enumerate(queries):
        if not q.get("label"):
            return {"error": True, "message": f"Query {i} is missing a 'label'."}
        if not q.get("model"):
            return {"error": True, "message": f"Query '{q.get('label', i)}' is missing a 'model'."}

    results: dict[str, Any] = {}
    successful = 0
    failed = 0

    for key in target_keys:
        try:
            conn = _get_connection(config, key)
        except ValueError as e:
            results[key] = {"error": str(e), "queries": {}}
            failed += 1
            if stop_on_error:
                break
            continue

        conn_result: dict[str, Any] = {
            "url": conn.get("url", ""),
            "db": conn.get("db", ""),
            "queries": {},
            "errors": [],
        }

        conn_ok = True
        for q in queries:
            label = q["label"]
            model = q["model"]
            method = q.get("method", "search_read")
            domain = q.get("domain", [])
            fields = q.get("fields", [])
            limit = q.get("limit", DEFAULT_LIMIT)
            offset = q.get("offset", 0)
            order = q.get("order", "")
            groupby = q.get("groupby", [])
            extra_kw = q.get("kwargs", {})
            context = q.get("context", {})

            try:
                if method == "search_read":
                    if not fields:
                        fields = ["id", "display_name"]
                    cap = HARD_CAP_NARROW if len(fields) <= NARROW_FIELD_THRESHOLD else HARD_CAP_WIDE
                    effective_limit = min(limit, cap) if limit > 0 else min(DEFAULT_LIMIT, cap)
                    kw: dict[str, Any] = {
                        "limit": effective_limit,
                        "offset": offset,
                        "fields": fields,
                        **extra_kw,
                    }
                    if order:
                        kw["order"] = order
                    ctx = _build_context(context)
                    if ctx:
                        kw["context"] = ctx

                    records = _execute(conn, model, "search_read", domain, **kw)
                    if isinstance(records, dict) and records.get("error"):
                        conn_result["queries"][label] = records
                        conn_result["errors"].append({"label": label, "error": records.get("message", str(records))})
                    else:
                        total_kw: dict[str, Any] = {}
                        if ctx:
                            total_kw["context"] = ctx
                        total = _execute(conn, model, "search_count", domain, **total_kw)
                        if isinstance(total, dict) and total.get("error"):
                            total = -1
                        conn_result["queries"][label] = {
                            "records": records,
                            "metadata": {
                                "count": len(records),
                                "total": total,
                                "offset": offset,
                                "limit": effective_limit,
                                "has_more": (offset + len(records)) < total if isinstance(total, int) else False,
                            },
                        }

                elif method == "search_count":
                    kw = {**extra_kw}
                    ctx = _build_context(context)
                    if ctx:
                        kw["context"] = ctx
                    count = _execute(conn, model, "search_count", domain, **kw)
                    if isinstance(count, dict) and count.get("error"):
                        conn_result["queries"][label] = count
                        conn_result["errors"].append({"label": label, "error": count.get("message", str(count))})
                    else:
                        conn_result["queries"][label] = {"count": count}

                elif method == "read_group":
                    kw = {**extra_kw}
                    if limit > 0:
                        kw["limit"] = limit
                    ctx = _build_context(context)
                    if ctx:
                        kw["context"] = ctx
                    group_result = _execute(conn, model, "read_group", domain, fields, groupby, **kw)
                    if isinstance(group_result, dict) and group_result.get("error"):
                        conn_result["queries"][label] = group_result
                        conn_result["errors"].append(
                            {"label": label, "error": group_result.get("message", str(group_result))}
                        )
                    else:
                        conn_result["queries"][label] = {
                            "groups": group_result,
                            "count": len(group_result) if isinstance(group_result, list) else 0,
                        }

                else:
                    kw = {**extra_kw}
                    ctx = _build_context(context)
                    if ctx:
                        kw["context"] = ctx
                    args = [domain] if domain else []
                    raw = _execute(conn, model, method, *args, **kw)
                    conn_result["queries"][label] = {"result": raw}

            except Exception as exc:
                err_msg = f"{type(exc).__name__}: {str(exc)[:300]}"
                conn_result["queries"][label] = {"error": True, "message": err_msg}
                conn_result["errors"].append({"label": label, "error": err_msg})

        if conn_result["errors"]:
            conn_ok = False

        if conn_ok:
            successful += 1
        else:
            if conn_result["queries"]:
                successful += 1
            failed += 1

        if not conn_result["errors"]:
            del conn_result["errors"]

        results[key] = conn_result

        if stop_on_error and not conn_ok:
            break

    return {
        "results": results,
        "summary": {
            "total_connections": len(target_keys),
            "queried": len(results),
            "successful": successful,
            "failed": failed,
        },
    }


# =============================================================================
# AP WORKER (existing)
# =============================================================================


@mcp.tool()
def odoo_trigger_ap_worker(doc_id: int, target_key: str = "") -> dict:
    """
    Trigger the AP Bill OCR Worker to process a document by its Odoo document ID.
    Requires ODOO_AP_WORKER_URL and ODOO_AP_WORKER_SECRET environment variables.
    """
    import urllib.error
    import urllib.request

    worker_url = os.environ.get("ODOO_AP_WORKER_URL", "").rstrip("/")
    worker_secret = os.environ.get("ODOO_AP_WORKER_SECRET", "")

    if not worker_url:
        return {"error": "ODOO_AP_WORKER_URL environment variable not set."}

    payload: dict[str, Any] = {"doc_id": doc_id}
    if target_key:
        payload["target_key"] = target_key

    data = json.dumps(payload).encode()
    req = urllib.request.Request(  # noqa: S310
        f"{worker_url}/webhook/document-upload",
        data=data,
        headers={
            "Content-Type": "application/json",
            "x-worker-secret": worker_secret,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return {"status": resp.status, "response": resp.read().decode()}
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "response": e.read().decode()}


# -- Health check --------------------------------------------------------------


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


# -- Entry point ---------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="sse")
