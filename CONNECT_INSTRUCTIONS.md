# How to Connect to the Odoo MCP Server

The Odoo MCP Server is deployed on Cloud Run. You do **not** need to install Python or any dependencies on your computer. You simply need to configure your AI assistant (Claude Desktop or Cursor) to connect to the deployed server.

**No Google Cloud account or access is required for users.** The server is publicly accessible but protected by a secret token in the URL.


## Connection Details

- **Server Type:** SSE (Server-Sent Events)
- **URL:** `https://odoo-mcp-server-njiacix2yq-as.a.run.app/sse`

---

## How to Use (Authentication)

Once connected, you can use the server with any Odoo instance by providing your credentials to the AI.

1.  **Start a chat** (in Claude or Cursor).
2.  **Tell the AI to connect:**
    > "Please connect to my Odoo instance at `https://my-company.odoo.com` using user `admin` and API key `xxx-your-api-key-xxx`."
3.  The AI will verify the connection.
4.  You can then ask questions like:
    > "Find the partner with email alice@example.com"
    > "Create a new lead..."

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
         "url": "https://odoo-mcp-server-njiacix2yq-as.a.run.app/mcp/km46op1ljw9c8syugv7adx30qe25birn",
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
         "url": "https://odoo-mcp-server-njiacix2yq-as.a.run.app/mcp/km46op1ljw9c8syugv7adx30qe25birn",
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
5. Enter the URL: `https://odoo-mcp-server-njiacix2yq-as.a.run.app/mcp/km46op1ljw9c8syugv7adx30qe25birn`
6. Click **Save**.

---

## Option 3: Other LLMs / MCP Clients

Any client that supports the **Model Context Protocol (MCP)** via **SSE (Server-Sent Events)** can use this server.

- **Transport:** SSE
- **URL:** `https://odoo-mcp-server-njiacix2yq-as.a.run.app/sse`
