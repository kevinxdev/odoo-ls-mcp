# How-to Guide: OpenCode Integration

This guide shows you how to integrate `odoo-ls-mcp` with OpenCode to empower your AI agents with Odoo-specific code intelligence.

## Prerequisites

1.  **OdooLS Server**: Ensure `odoo_ls_server` is installed.
    ```bash
    odoo_ls_server --version  # Should return v1.3.1 or similar
    ```
2.  **odoo-ls-mcp**: Clone and sync the repository as described in the [README](../README.md).

## Step 1: Register the MCP Server

OpenCode manages MCP servers through `~/.config/opencode/opencode.json`. You need to add `odoo-ls` to the `mcp` object.

1.  Open `~/.config/opencode/opencode.json` in your editor.
2.  Add the following configuration:

```json
{
  "mcp": {
    "odoo-ls": {
      "type": "local",
      "command": ["/home/kevin/Development/odoo-ls-mcp/.venv/bin/odoo-ls-mcp"],
      "environment": {
        "ODOO_LS_PATH": "/home/kevin/.local/bin/odoo_ls_server"
      },
      "enabled": true
    }
  }
}
```

3.  **Restart OpenCode** (or the current session) to apply the changes.

## Step 2: Verify the Connection

Once registered, you can verify the server is working by calling the `check_odools_available` tool.

**Example Request:**
> "Use the odoo-ls MCP server to check if it's available."

**Expected Result:**
```text
✅ odoo_ls_server found at: /home/kevin/.local/bin/odoo_ls_server
odoo_ls_server 1.3.1
```

## Step 3: Run Your First Analysis

To get diagnostics for your Odoo workspace, use the `parse_diagnostics` tool.

**Example Request:**
> "Run diagnostics on the workspace at /home/kevin/Development/Odoo/athenrix-docker-base"

**Tool Output:**
The server will return a formatted list of warnings and errors found in your Odoo addons, such as missing dependencies, XML ID mismatches, or Python linting issues.

## FAQ

### Why am I getting "No odools.toml found"?
OdooLS requires a configuration file to know where the Odoo source and addons are. Ensure an `odools.toml` exists in your workspace root. You can use `list_odools_config` to see which configs the server can find.

### How do I use Go-to-Definition?
1.  First, call `start_session(workspace="/your/path")`. This starts a live LSP process.
2.  Then, use `go_to_definition(workspace="/your/path", file_path="/your/file.py", line=10, character=5)`.
3.  The tool will return the exact file and position where the symbol is defined.

### Can I change the session timeout?
Yes. Set the `ODOO_LS_IDLE_TTL` environment variable in your `opencode.json` configuration. The default is 300 seconds (5 minutes).

## Troubleshooting

- **Server Timeout**: If your workspace is very large, initial indexing might take several minutes. Use `session_health` to check if indexing is still in progress.
- **Path Issues**: Always use **absolute paths** for `command` and `environment` variables in `opencode.json`.
- **Logs**: Check OpenCode's logs for any error messages emitted by the MCP server to its stderr.
