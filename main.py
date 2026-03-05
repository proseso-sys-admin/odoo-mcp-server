#!/usr/bin/env python3
"""
Odoo MCP Server - Cloud Run HTTP edition.

Credentials are loaded from environment variables (set via Cloud Run secrets):
  ODOO_CONNECTIONS  - JSON string of the full connections.json config
  MCP_SECRET        - Secret path segment for basic endpoint protection

Run locally:
  MCP_SECRET=dev ODOO_CONNECTIONS='{"connections":{"default":{...}},"default":"default"}' \
  python main.py

Dynamic connections:
  Pass a JSON string as the connection parameter instead of a named key:
  '{"url": "https://client.odoo.com", "user": "admin@example.com", "api_key": "xxx"}'
  The db name is auto-derived from the URL subdomain if not provided.
"""

import json
import os
import xmlrpc.client
from typing import Any
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import PlainTextResponse

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
    """
    Resolve a connection by name OR by inline JSON spec.

    Named key (existing behaviour):
        "my-company"

    Inline JSON (new - dynamic connections):
        '{"url": "https://client.odoo.com", "user": "admin@x.com", "api_key": "abc123"}'
        '{"url": "...", "db": "client-db", "user": "...", "api_key": "..."}'

    When 'db' is omitted from an inline spec the first subdomain segment of the
    URL is used (e.g. https://the-digital-hotelier.odoo.com -> the-digital-hotelier).
    """
    conns = config.get("connections", {})

    # 1. Named connection (original behaviour)
    if key in conns:
        return conns[key]

    # 2. Inline JSON connection spec
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
        common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common", allow_none=True)
        uid = common.authenticate(db, user, api_key, {})
        if not uid:
            raise ValueError(
                f"Authentication failed for {user}@{db} on {url}. "
                "Check URL, database name, user login, and API key."
            )
        _uid_cache[cache_key] = uid

    return url, _uid_cache[cache_key], api_key


def _execute(conn: dict, model: str, method: str, *args, **kwargs) -> Any:
    url, uid, api_key = _authenticate(conn)
    db = conn["db"]
    obj = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", allow_none=True)
    return obj.execute_kw(db, uid, api_key, model, method, list(args), kwargs)


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
def odoo_authenticate(url: str, user: str, api_key: str, db: str = "") -> str:
    """
    Validate credentials and return the connection string to use for other tools.
    Use this if you don't have a pre-configured connection.
    
    Returns a JSON string that should be passed as the 'connection' argument 
    to all other Odoo tools.
    """
    if not db:
        host = urlparse(url).hostname or ""
        db = host.split(".")[0]
        
    conn = {"url": url, "db": db, "user": user, "api_key": api_key}
    
    # Verify credentials immediately
    _authenticate(conn)
    
    return json.dumps(conn)


@mcp.tool()
def odoo_search(
    connection: str,
    model: str,
    domain: list = [],
    fields: list = [],
    limit: int = 80,
    offset: int = 0,
    order: str = "",
    company_id: int = 0,
) -> list:
    """Search Odoo records (search_read). Returns records matching the domain filter."""
    config = load_config()
    conn = _get_connection(config, connection)
    kwargs: dict[str, Any] = {"limit": limit, "offset": offset}
    if fields:
        kwargs["fields"] = fields
    if order:
        kwargs["order"] = order
    if company_id:
        kwargs["context"] = {"allowed_company_ids": [company_id]}
    return _execute(conn, model, "search_read", domain, **kwargs)


@mcp.tool()
def odoo_read(
    connection: str,
    model: str,
    ids: list,
    fields: list = [],
    company_id: int = 0,
) -> list:
    """Read specific Odoo records by ID."""
    config = load_config()
    conn = _get_connection(config, connection)
    kwargs: dict[str, Any] = {}
    if fields:
        kwargs["fields"] = fields
    if company_id:
        kwargs["context"] = {"allowed_company_ids": [company_id]}
    return _execute(conn, model, "read", ids, **kwargs)


@mcp.tool()
def odoo_create(
    connection: str,
    model: str,
    vals: dict,
    company_id: int = 0,
) -> dict:
    """Create a new Odoo record. Returns the new record ID."""
    config = load_config()
    conn = _get_connection(config, connection)
    kwargs: dict[str, Any] = {}
    if company_id:
        kwargs["context"] = {"allowed_company_ids": [company_id]}
    new_id = _execute(conn, model, "create", vals, **kwargs)
    return {"id": new_id}


@mcp.tool()
def odoo_write(
    connection: str,
    model: str,
    ids: list,
    vals: dict,
    company_id: int = 0,
) -> dict:
    """Update existing Odoo records."""
    config = load_config()
    conn = _get_connection(config, connection)
    kwargs: dict[str, Any] = {}
    if company_id:
        kwargs["context"] = {"allowed_company_ids": [company_id]}
    result = _execute(conn, model, "write", ids, vals, **kwargs)
    return {"success": result, "updated_ids": ids}


@mcp.tool()
def odoo_call(
    connection: str,
    model: str,
    method: str,
    args: list = [],
    kwargs: dict = {},
    company_id: int = 0,
) -> dict:
    """Call any Odoo method directly (execute_kw). Use for action_post, action_register_payment, unlink, etc."""
    config = load_config()
    conn = _get_connection(config, connection)
    kw = dict(kwargs)
    if company_id:
        ctx = kw.get("context", {})
        ctx["allowed_company_ids"] = [company_id]
        kw["context"] = ctx
    result = _execute(conn, model, method, *args, **kw)
    return {"result": result}


@mcp.tool()
def odoo_count(
    connection: str,
    model: str,
    domain: list = [],
    company_id: int = 0,
) -> dict:
    """Count records matching a domain filter."""
    config = load_config()
    conn = _get_connection(config, connection)
    kwargs: dict[str, Any] = {}
    if company_id:
        kwargs["context"] = {"allowed_company_ids": [company_id]}
    count = _execute(conn, model, "search_count", domain, **kwargs)
    return {"count": count}


@mcp.tool()
def odoo_get_fields(
    connection: str,
    model: str,
    attributes: list = ["string", "type", "required", "relation"],
) -> dict:
    """Get field definitions for an Odoo model."""
    config = load_config()
    conn = _get_connection(config, connection)
    return _execute(conn, model, "fields_get", [], attributes=attributes)


@mcp.tool()
def odoo_list_companies(connection: str) -> list:
    """List all companies in an Odoo database (for multi-company setups)."""
    config = load_config()
    conn = _get_connection(config, connection)
    return _execute(
        conn, "res.company", "search_read", [],
        fields=["id", "name", "currency_id", "country_id"]
    )


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
