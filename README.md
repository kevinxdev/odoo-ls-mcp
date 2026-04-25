# odoo-ls-mcp

MCP server that bridges [OdooLS](https://github.com/odoo/odoo-ls) (the Odoo Language Server) to LLM agents via the [Model Context Protocol](https://modelcontextprotocol.io).

This server exposes Odoo-specific intelligence — static analysis, diagnostics, hover, go-to-definition, and completions — as MCP tools callable by Claude, Cursor, Copilot, and other MCP-aware agents.

## Prerequisites

- **Python 3.11+**
- **OdooLS binary**: `odoo_ls_server` (v1.3.1+ recommended).
- **Workspace Config**: An `odools.toml` file in your Odoo workspace root.

## Installation

```bash
# Clone the repository
git clone https://github.com/your-org/odoo-ls-mcp
cd odoo-ls-mcp

# Install dependencies and create virtual environment
uv sync

# Activate the venv (optional for CLI usage, required for some environments)
source .venv/bin/activate
```

## OpenCode Registration

To register this server in OpenCode, add the following entry to your `~/.config/opencode/opencode.json` file. Replace paths with the absolute paths on your machine.

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

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `ODOO_LS_PATH` | Absolute path to the `odoo_ls_server` binary. | `odoo_ls_server` (from PATH) |
| `ODOO_LS_LOG_LEVEL` | Log level for the MCP server (DEBUG, INFO, WARNING, ERROR). | `WARNING` |
| `ODOO_LS_IDLE_TTL` | Seconds to keep an idle OdooLS session alive before shutdown. | `1800` |
| `ODOO_LS_PREVIEW_LENGTH` | Character limit for docstring/hover previews. | `120` |
| `ODOO_LS_WORKSPACE_ROOT` | Default workspace root if not provided in tool calls. | None |
| `ODOO_LS_CONFIG_PATH` | Explicit path to `odools.toml` to override auto-discovery. | None |

## Available Tools

| Tool | Description |
|------|-------------|
| `check_odools_available` | Verify that `odoo_ls_server` is installed and reachable. |
| `list_odools_config` | Search upward for `odools.toml` files from a path. |
| `inspect_workspace_config` | Display the resolved `odools.toml` settings for a workspace. |
| `parse_diagnostics` | Run one-shot static analysis of a workspace. |
| `start_session` | Start a live LSP session (required for navigation tools). |
| `hover` | Get type, documentation, and signature at a specific position. |
| `go_to_definition` | Find the definition location of a symbol. |
| `find_references` | Find all usages of a symbol across the workspace. |
| `document_symbols` | List all classes, methods, and fields in a specific file. |
| `workspace_symbols` | Search for symbols matching a query across the entire workspace. |
| `lookup_model` | Fast grep-based finder for Odoo model definitions. |
| `lookup_xmlid` | Fast grep-based finder for XML ID definitions. |
| `session_health` | Report health and status of all active OdooLS sessions. |

## Workspace Config (odools.toml)

OdooLS requires an `odools.toml` file to understand your Odoo environment. The server automatically walks up the directory tree from your workspace root to find this file.

Example `odools.toml` from `/home/kevin/Development/Odoo/athenrix-docker-base/odools.toml`:

```toml
[[config]]
odoo_path = "/home/kevin/Development/Odoo/athenrix-docker-base/odoo/custom/src/odoo"
addons_paths = ["$autoDetectAddons"]
```

## Usage

Typical flow: verify OdooLS, start a workspace session, then use navigation and lookup tools against the same workspace.

1. `check_odools_available` → confirm `odoo_ls_server` is installed.
2. `start_session` with workspace `/home/kevin/Development/Odoo/athenrix-docker-base`.
3. `hover` to inspect the symbol under the cursor.
4. `go_to_definition` to jump to the symbol's definition.
5. `find_references` to list all usages.
6. `lookup_model` to locate an Odoo model such as `sale.order`.
7. `lookup_xmlid` to resolve an XML ID such as `sale.action_orders`.

```text
check_odools_available()
start_session(workspace="/home/kevin/Development/Odoo/athenrix-docker-base")
hover(
  workspace="/home/kevin/Development/Odoo/athenrix-docker-base",
  file_path="/home/kevin/Development/Odoo/athenrix-docker-base/odoo/custom/src/private/my_addon/models/example.py",
  line=12,
  character=8,
)
go_to_definition(...same workspace/file/position...)
find_references(...same workspace/file/position...)
lookup_model(workspace="/home/kevin/Development/Odoo/athenrix-docker-base", model="sale.order")
lookup_xmlid(workspace="/home/kevin/Development/Odoo/athenrix-docker-base", xmlid="sale.action_orders")
```

## Troubleshooting

- **Binary not found**: Ensure `ODOO_LS_PATH` is correct or `odoo_ls_server` is in your system PATH.
- **Missing `odools.toml`**: The server will fail with a `ConfigError` if no config is found. Create one in your workspace root.
- **Session not starting**: Check stderr logs (visible in OpenCode) for LSP handshake failures or OdooLS crashes.
- **OdooLS still indexing**: Large workspaces take time to index. Use `indexing_status` or `session_health` to check if the server is READY.
- **Stdout contamination**: All logging is directed to stderr. Never use `print()` in server code as it breaks the MCP protocol.

## Scope Limits

- **Read-only**: This server is for navigation and diagnostics. It does not perform code changes.
- **No Rename**: Renaming symbols is not supported.
- **No Code Actions**: "Quick fix" suggestions are not implemented.
- **No Formatting**: Formatting tools are not provided; use your project's existing linting tasks.
- **No Implementation/Call Hierarchy**: Advanced LSP features like call hierarchy are currently out of scope.

## Development & Testing

```bash
# Run full test suite
uv run pytest tests/ -q

# Run unit and integration tests (skipping slow E2E)
uv run pytest tests/ -m "not e2e" -q

# Run E2E tests only
uv run pytest tests/ -m e2e -q
```
