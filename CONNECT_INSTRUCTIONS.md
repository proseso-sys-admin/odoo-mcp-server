# How to Connect to the Odoo MCP Server (v2)

The Odoo MCP Server is deployed on Cloud Run. You do **not** need to install Python or any dependencies on your computer. You simply need to configure your AI assistant (Claude Desktop, Cursor, or any MCP client) to connect to the deployed server.

**No Google Cloud account or access is required for users.** The server is publicly accessible.


## Connection Details

- **Server Type:** SSE (Server-Sent Events)
- **URL:** `https://odoo-mcp-server-njiacix2yq-as.a.run.app/sse`
https://odoo-mcp-server-njiacix2yq-as.a.run.app/sse
---

## How to Use (Authentication)

Once connected, authenticate with any Odoo instance by providing your credentials to the AI.

1.  **Start a chat** (in Claude or Cursor).
2.  **Tell the AI to connect:**
    > "Please connect to my Odoo instance at `https://my-company.odoo.com` using user `admin` and API key `xxx-your-api-key-xxx`."
3.  The AI will call `odoo_authenticate` and verify the connection.
4.  You can then ask questions like:
    > "Find the partner with email alice@example.com"
    > "What's our total revenue this quarter, grouped by salesperson?"
    > "Show me the form view fields for account.move"

### Password Login

You can also authenticate with a password instead of an API key:
> "Connect to `https://my-company.odoo.com` with user `admin` and password `mypassword`."

### JSON-2 API (Odoo 19+)

For Odoo 19+ instances, you can use the new JSON-2 API for cleaner errors and native JSON:
> "Connect to `https://my-company.odoo.com` with user `admin`, API key `xxx`, and use the JSON-2 transport."

The AI will pass `transport="json2"` to `odoo_authenticate`.

---

## Available Tools (v2)

The server exposes **30+ tools** organized by category:

### Core CRUD (with context-window protection)
| Tool | Description |
|------|-------------|
| `odoo_search` | Search records with smart pagination. Returns metadata (`total`, `has_more`, `next_offset`). Defaults to `['id', 'display_name']` if no fields specified. Auto-caps at 100 (or 1000 for narrow queries). |
| `odoo_read` | Read specific records by ID. Defaults to `['id', 'display_name']`. |
| `odoo_create` | Create a new record. |
| `odoo_write` | Update existing records. |
| `odoo_delete` | Safe record deletion with protected-model guard. |
| `odoo_copy` | Duplicate a record with optional field overrides. |
| `odoo_count` | Count records matching a domain filter. |
| `odoo_call` | Call any Odoo method directly (fallback for unlisted methods). |

### Advanced ORM
| Tool | Description |
|------|-------------|
| `odoo_read_group` | SQL GROUP BY aggregation (sums, counts, groupby). |
| `odoo_name_search` | Fuzzy Many2one-style lookup by display name. |
| `odoo_name_create` | Quick-create a record by display name only. |
| `odoo_default_get` | Preview auto-fill defaults before creating a record. |
| `odoo_get_metadata` | Record audit info (who created/modified, xmlid). |

### Schema Discovery & Introspection
| Tool | Description |
|------|-------------|
| `odoo_search_models` | Find models by name or description (e.g., "invoice" → `account.move`). |
| `odoo_get_fields` | Get field definitions, filterable by `search_term` or `field_type`. |
| `odoo_get_views` | Fetch form/list/kanban XML architecture to know which fields the UI shows. |
| `odoo_get_menus` | Query menu structure and window actions for a model. |
| `odoo_check_access` | Check current user's read/write/create/unlink permissions on a model. |
| `odoo_list_companies` | List all companies (multi-company setups). |

### Batch Execution
| Tool | Description |
|------|-------------|
| `odoo_execute_batch` | Run multiple operations in a single tool call. |

### Files & Reports
| Tool | Description |
|------|-------------|
| `odoo_upload_attachment` | Upload a file (base64) to `ir.attachment`. |
| `odoo_download_attachment` | Download an attachment (returns base64 + metadata). |
| `odoo_get_report` | Generate a PDF report (invoice, delivery slip, etc.). |

### Server Actions & Crons
| Tool | Description |
|------|-------------|
| `odoo_run_server_action` | Execute an `ir.actions.server` record. |
| `odoo_list_crons` | List scheduled actions (`ir.cron`). |
| `odoo_trigger_cron` | Manually trigger a cron to run immediately. |

### Custom Fields (Studio-less)
| Tool | Description |
|------|-------------|
| `odoo_create_custom_field` | Create `x_` custom fields via `ir.model.fields`. |

### Context Parameter

All tools accept a `context: dict` parameter supporting:
- `{"active_test": False}` — include archived/inactive records
- `{"lang": "es_ES"}` — execute in a specific language
- `{"allowed_company_ids": [1, 2]}` — multi-company context

---

## Option 1: Claude Desktop App

1. Open your Claude Desktop configuration file.

   **Standard Windows Installation:**
   - Open File Explorer and paste this into the address bar: `%APPDATA%\Claude\claude_desktop_config.json`

   **Windows Store Installation (if the above doesn't exist):**
   - It may be located at: `%LOCALAPPDATA%\Packages\Claude_...\LocalCache\Roaming\Claude\claude_desktop_config.json`

   **macOS:**
   - `~/Library/Application Support/Claude/claude_desktop_config.json`

2. Edit the file to include the `mcpServers` section.

   **If your file is empty or only has `{}`:**
   ```json
   {
     "mcpServers": {
       "odoo-connect": {
         "command": "",
         "url": "https://odoo-mcp-server-njiacix2yq-as.a.run.app/sse",
         "transport": "sse"
       }
     }
   }
   ```

   **If your file already has content (like "preferences"), add "mcpServers" as a new key:**
   ```json
   {
     "preferences": {
       ...
     },
     "mcpServers": {
       "odoo-connect": {
         "command": "",
         "url": "https://odoo-mcp-server-njiacix2yq-as.a.run.app/sse",
         "transport": "sse"
       }
     }
   }
   ```

3. Restart Claude Desktop.

---

## Option 2: Cursor (AI Code Editor)

1. Open **Cursor Settings** > **Features** > **MCP**.
2. Click **+ Add New MCP Server**.
3. Select **SSE** as the type.
4. Enter the Name: `odoo-connect`
5. Enter the URL: `https://odoo-mcp-server-njiacix2yq-as.a.run.app/sse`
6. Click **Save**.

---

## Option 3: Other LLMs / MCP Clients

Any client that supports the **Model Context Protocol (MCP)** via **SSE (Server-Sent Events)** can use this server.

- **Transport:** SSE
- **URL:** `https://odoo-mcp-server-njiacix2yq-as.a.run.app/sse`
