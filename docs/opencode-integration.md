# OpenCode Integration Guide

This guide explains how to integrate the `odoo-ls-mcp` server with OpenCode to provide Odoo-specific intelligence to your AI agents.

## Installation

Ensure you have the Odoo Language Server (`odoo_ls_server`) installed and accessible.

```bash
# Recommended: install into a local virtual environment
cd ~/Development/odoo-ls-mcp
uv sync
```

## OpenCode Registration

OpenCode uses a central configuration file at `~/.config/opencode/opencode.json`. To register the OdooLS MCP server, add an entry to the `mcp` object.

### Registration Snippet

Copy and paste the following into your `opencode.json`:

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
| `ODOO_LS_PATH` | Path to the `odoo_ls_server` binary. | `odoo_ls_server` (from PATH) |

## Workspace Configuration

The OdooLS server requires an `odools.toml` file at your workspace root (e.g., `/home/kevin/Development/Odoo/athenrix-docker-base/odools.toml`).

Example `odools.toml`:

```toml
[[config]]
odoo_path = "/home/kevin/Development/Odoo/athenrix-docker-base/odoo/custom/src/odoo"
addons_paths = ["$autoDetectAddons"]
```

## Tool Reference

| Tool | Purpose |
|------|---------|
| `check_odools_available` | Verifies binary and version info. |
| `list_odools_config` | Locates and previews `odools.toml`. |
| `inspect_workspace_config` | Displays the resolved configuration. |
| `parse_diagnostics` | Runs one-shot static analysis. |
| `start_session` | Initializes a live LSP session (required for navigation). |
| `hover` | Gets type/docs at a specific position. |
| `go_to_definition` | Finds symbol definitions. |
| `find_references` | Finds all usages of a symbol. |
| `document_symbols` | Lists symbols in a specific file. |
| `workspace_symbols` | Searches symbols across the workspace. |
| `lookup_model` | Fast grep-based model finder. |
| `lookup_xmlid` | Fast grep-based XML ID finder. |
| `session_health` | Reports health of active sessions. |

## Troubleshooting

- **Server fails to start:** Verify the `command` path in `opencode.json` points to the correct executable in your `.venv`.
- **No diagnostics/navigation:** Ensure `odools.toml` is present and `odoo_path` is correct.
- **Tools not visible:** Check OpenCode logs to ensure the MCP server was loaded without errors.

## Scope Limits

- **Navigation and Diagnostics only:** This integration focuses on reading and understanding code.
- **No Refactoring:** `rename` and `code actions` are not currently supported.
- **No Formatting:** Use existing repository `invoke lint` tasks for formatting.
- **LSP Support:** Limited to the capabilities provided by `odoo_ls_server`.
