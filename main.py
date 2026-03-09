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
import xmlrpc.client
from typing import Any
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import PlainTextResponse

# -- Constants -----------------------------------------------------------------

DEFAULT_LIMIT = 100
HARD_CAP_NARROW = 1000   # max when fields <= 5
HARD_CAP_WIDE = 100      # max when fields > 5 or unspecified
NARROW_FIELD_THRESHOLD = 5

PROTECTED_MODELS = frozenset({
    "ir.model", "ir.module.module", "res.company", "base",
})

# -- Config --------------------------------------------------------------------

MCP_SECRET = os.environ.get("MCP_SECRET", "")


def load_config() -> dict:
    raw = os.environ.get("ODOO_CONNECTIONS", "")
    if raw:
        return json.loads(raw)
    return {"connections": {}, "default": None}


# -- Odoo XML-RPC helpers ------------------------------------------------------

_uid_cache: dict[str, int] = {}


def _get_connection(config: dict, key: str) -> dict:
    """Resolve a connection by name OR by inline JSON spec."""
    conns = config.get("connections", {})

    if key in conns:
        return conns[key]

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

    raise ValueError(
        f"Connection '{key}' not found. Available: {list(conns.keys())}"
    )


def _authenticate(conn: dict) -> tuple[str, int, str]:
    url = conn["url"].rstrip("/")
    db = conn["db"]
    user = conn.get("user", "admin")
    api_key = conn["api_key"]

    cache_key = f"{url}|{db}|{user}"
    if cache_key not in _uid_cache:
        common = xmlrpc.client.ServerProxy(
            f"{url}/xmlrpc/2/common", allow_none=True
        )
        uid = common.authenticate(db, user, api_key, {})
        if not uid:
            raise ValueError(
                f"Authentication failed for {user}@{db} on {url}. "
                "Check URL, database name, user login, and API key."
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
    obj = xmlrpc.client.ServerProxy(
        f"{url}/xmlrpc/2/object", allow_none=True
    )
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

    field_match = re.search(
        r"Invalid field '(\w+)' on model '([\w.]+)'", msg
    )
    if field_match:
        result["message"] = (
            f"Field '{field_match.group(1)}' does not exist on model "
            f"'{field_match.group(2)}'."
        )
        result["hint"] = (
            "Call odoo_get_fields to discover valid field names for this model."
        )
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


# -- MCP Server ----------------------------------------------------------------

_port = int(os.environ.get("PORT", "8080"))
mcp = FastMCP(
    "odoo-connect",
    host="0.0.0.0",
    port=_port,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    ),
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
            k: {"url": v.get("url"), "db": v.get("db"), "user": v.get("user", "admin")}
            for k, v in conns.items()
        },
        "default": config.get("default"),
    }


@mcp.tool()
def odoo_authenticate(url: str, user: str, api_key: str, db: str = "", transport: str = "xmlrpc") -> str:
    """
    Validate credentials and return a connection JSON string for all other tools.
    Set transport='json2' to use the Odoo 19+ JSON-2 API instead of XML-RPC.
    """
    if not db:
        host = urlparse(url).hostname or ""
        db = host.split(".")[0]

    conn = {"url": url, "db": db, "user": user, "api_key": api_key, "transport": transport}
    _authenticate(conn)
    return json.dumps(conn)


# =============================================================================
# CORE CRUD — with context-window protection
# =============================================================================

@mcp.tool()
def odoo_search(
    connection: str,
    model: str,
    domain: list = [],
    fields: list = [],
    limit: int = 0,
    offset: int = 0,
    order: str = "",
    context: dict = {},
) -> dict:
    """
    Search Odoo records (search_read) with smart pagination.

    Returns {"records": [...], "metadata": {count, total, has_more, next_offset}}.
    Default fields are ['id', 'display_name'] if omitted.
    Limit is auto-capped to protect context: 100 for wide queries, 1000 for narrow (<=5 fields).
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
        meta["warning"] = (
            f"Limit capped to {cap}. Request <=5 fields to allow up to {HARD_CAP_NARROW}."
        )

    return {"records": records, "metadata": meta}


@mcp.tool()
def odoo_read(
    connection: str,
    model: str,
    ids: list,
    fields: list = [],
    context: dict = {},
) -> list:
    """Read specific Odoo records by ID. Defaults to ['id', 'display_name'] if fields omitted."""
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
    connection: str,
    model: str,
    vals: dict,
    context: dict = {},
) -> dict:
    """Create a new Odoo record. Returns the new record ID."""
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
def odoo_write(
    connection: str,
    model: str,
    ids: list,
    vals: dict,
    context: dict = {},
) -> dict:
    """Update existing Odoo records."""
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
    connection: str,
    model: str,
    ids: list,
    context: dict = {},
) -> dict:
    """
    Delete Odoo records (unlink). Refuses to delete records from protected system models.
    Returns confirmation with count of deleted records.
    """
    if model in PROTECTED_MODELS:
        return {
            "error": True,
            "message": f"Deletion from '{model}' is blocked for safety. "
                       "Use odoo_call if you truly need this."
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
    connection: str,
    model: str,
    id: int,
    default: dict = {},
    context: dict = {},
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
    connection: str,
    model: str,
    domain: list = [],
    context: dict = {},
) -> dict:
    """Count records matching a domain filter."""
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
    connection: str,
    model: str,
    method: str,
    args: list = [],
    kwargs: dict = {},
    context: dict = {},
) -> dict:
    """
    Call any Odoo method directly (execute_kw).
    Use for action_post, action_register_payment, or any method not covered by other tools.
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
# ADVANCED ORM TOOLS
# =============================================================================

@mcp.tool()
def odoo_read_group(
    connection: str,
    model: str,
    domain: list = [],
    fields: list = [],
    groupby: list = [],
    orderby: str = "",
    limit: int = 0,
    lazy: bool = True,
    context: dict = {},
) -> dict:
    """
    Aggregate records using read_group (like SQL GROUP BY).
    Example: fields=['amount_total:sum'], groupby=['partner_id', 'date:month']
    Returns grouped results with __count and aggregated values.
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
    connection: str,
    model: str,
    name: str = "",
    domain: list = [],
    operator: str = "ilike",
    limit: int = 10,
    context: dict = {},
) -> list:
    """
    Fuzzy search by display name (like Odoo's Many2one dropdown).
    Returns [(id, display_name), ...]. Much faster than search_read for lookups.
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
def odoo_name_create(
    connection: str,
    model: str,
    name: str,
    context: dict = {},
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
    connection: str,
    model: str,
    fields: list = [],
    context: dict = {},
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
    connection: str,
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
    connection: str,
    query: str = "",
    limit: int = 20,
) -> list:
    """
    Search for Odoo models by name or description.
    Example: query='invoice' finds account.move. Returns technical name, label, and info.
    """
    conn = _conn(connection)
    domain = []
    if query:
        domain = [
            "|", "|",
            ("model", "ilike", query),
            ("name", "ilike", query),
            ("info", "ilike", query),
        ]
    return _execute(
        conn, "ir.model", "search_read", domain,
        fields=["model", "name", "info", "state", "transient"],
        limit=limit,
    )


@mcp.tool()
def odoo_get_fields(
    connection: str,
    model: str,
    attributes: list = ["string", "type", "required", "relation", "help"],
    search_term: str = "",
    field_type: str = "",
) -> dict:
    """
    Get field definitions for an Odoo model.
    Optionally filter by search_term (matches field name or label) or
    field_type (e.g., 'many2one', 'char', 'monetary', 'one2many').
    """
    conn = _conn(connection)
    all_fields = _execute(conn, model, "fields_get", [], attributes=attributes)

    if isinstance(all_fields, dict) and all_fields.get("error"):
        return all_fields

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


@mcp.tool()
def odoo_get_views(
    connection: str,
    model: str,
    view_type: str = "form",
    context: dict = {},
) -> dict:
    """
    Get the XML architecture of an Odoo view (form, list, search, kanban).
    The AI can read this XML to know exactly which fields the user sees in the UI.
    """
    conn = _conn(connection)
    kw: dict[str, Any] = {}
    ctx = _build_context(context)
    if ctx:
        kw["context"] = ctx
    try:
        result = _execute(
            conn, model, "get_views",
            [[False, view_type]],
            **kw,
        )
        if isinstance(result, dict) and not result.get("error"):
            views = result.get("views", {})
            view_data = views.get(view_type, {})
            return {
                "arch": view_data.get("arch", ""),
                "view_id": view_data.get("id"),
                "model": model,
                "type": view_type,
            }
        return result
    except Exception:
        result = _execute(
            conn, model, "fields_view_get",
            view_type=view_type,
        )
        if isinstance(result, dict) and not result.get("error"):
            return {
                "arch": result.get("arch", ""),
                "view_id": result.get("view_id"),
                "model": model,
                "type": view_type,
            }
        return result


@mcp.tool()
def odoo_get_menus(
    connection: str,
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
            conn, "ir.actions.act_window", "search_read",
            [("res_model", "=", model)],
            fields=["id", "name", "res_model", "view_mode", "domain", "context"],
            limit=limit,
        )
        if isinstance(actions, dict) and actions.get("error"):
            return actions

        action_ids = [
            f"ir.actions.act_window,{a['id']}" for a in actions
        ]
        if not action_ids:
            return {"message": f"No menu actions found for model '{model}'."}

        menus = _execute(
            conn, "ir.ui.menu", "search_read",
            [("action", "in", action_ids)],
            fields=["id", "name", "complete_name", "action", "parent_id"],
            limit=limit,
        )
        return {"actions": actions, "menus": menus}

    return _execute(
        conn, "ir.ui.menu", "search_read", [],
        fields=["id", "name", "complete_name", "parent_id", "action"],
        limit=limit,
    )


@mcp.tool()
def odoo_check_access(
    connection: str,
    model: str,
) -> dict:
    """
    Check current user's access rights on a model (read/write/create/unlink).
    Queries ir.model.access for the user's groups.
    """
    conn = _conn(connection)

    access_records = _execute(
        conn, "ir.model.access", "search_read",
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
def odoo_list_companies(connection: str) -> list:
    """List all companies in an Odoo database (for multi-company setups)."""
    conn = _conn(connection)
    return _execute(
        conn, "res.company", "search_read", [],
        fields=["id", "name", "currency_id", "country_id"],
    )


# =============================================================================
# BATCH EXECUTION
# =============================================================================

@mcp.tool()
def odoo_execute_batch(
    connection: str,
    operations: list,
) -> dict:
    """
    Execute multiple Odoo operations in a single tool call.
    Each operation: {"model": "...", "method": "...", "args": [...], "kwargs": {...}}
    Returns a list of results in the same order.
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
    connection: str,
    name: str,
    data_base64: str,
    res_model: str = "",
    res_id: int = 0,
    context: dict = {},
) -> dict:
    """
    Upload a file to Odoo as an ir.attachment.
    data_base64: the file content encoded as base64.
    Optionally attach to a record by setting res_model and res_id.
    """
    conn = _conn(connection)
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
    connection: str,
    attachment_id: int,
) -> dict:
    """
    Download an attachment from Odoo. Returns base64 data and filename.
    """
    conn = _conn(connection)
    records = _execute(
        conn, "ir.attachment", "read", [attachment_id],
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
    connection: str,
    report_name: str,
    record_ids: list,
    context: dict = {},
) -> dict:
    """
    Generate an Odoo PDF report (invoice, delivery slip, etc.).
    report_name: the technical report name (e.g., 'account.report_invoice').
    Returns base64-encoded PDF data.
    """
    conn = _conn(connection)
    url, uid, api_key = _authenticate(conn)
    db = conn["db"]

    report_obj = xmlrpc.client.ServerProxy(
        f"{url}/xmlrpc/2/report", allow_none=True
    )
    try:
        kw: dict[str, Any] = {}
        ctx = _build_context(context)
        if ctx:
            kw["context"] = ctx
        result = report_obj.render_report(
            db, uid, api_key, report_name, record_ids, kw
        )
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
    connection: str,
    action_id: int,
    context: dict = {},
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
    connection: str,
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
        conn, "ir.cron", "search_read", domain,
        fields=["id", "name", "model_id", "interval_number", "interval_type",
                "nextcall", "active", "numbercall"],
        limit=100,
    )


@mcp.tool()
def odoo_trigger_cron(
    connection: str,
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
    connection: str,
    model: str,
    name: str,
    field_type: str,
    string: str = "",
    required: bool = False,
    help: str = "",
    selection: list = [],
    relation: str = "",
    relation_field: str = "",
    domain: str = "",
    context: dict = {},
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
        conn, "ir.model", "search_read",
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
        vals["selection_ids"] = [
            (0, 0, {"value": s[0], "name": s[1]}) for s in selection
        ]
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
# AP WORKER (existing)
# =============================================================================

@mcp.tool()
def odoo_trigger_ap_worker(doc_id: int, target_key: str = "") -> dict:
    """
    Trigger the AP Bill OCR Worker to process a document by its Odoo document ID.
    Requires ODOO_AP_WORKER_URL and ODOO_AP_WORKER_SECRET environment variables.
    """
    import urllib.request
    import urllib.error

    worker_url = os.environ.get("ODOO_AP_WORKER_URL", "").rstrip("/")
    worker_secret = os.environ.get("ODOO_AP_WORKER_SECRET", "")

    if not worker_url:
        return {"error": "ODOO_AP_WORKER_URL environment variable not set."}

    payload: dict[str, Any] = {"doc_id": doc_id}
    if target_key:
        payload["target_key"] = target_key

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{worker_url}/webhook/document-upload",
        data=data,
        headers={
            "Content-Type": "application/json",
            "x-worker-secret": worker_secret,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
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
