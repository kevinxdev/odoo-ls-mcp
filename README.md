# odoo-ls-mcp

MCP server that bridges [OdooLS](https://github.com/odoo/odoo-ls) (the Odoo Language
Server) to LLM agents via the [Model Context Protocol](https://modelcontextprotocol.io).

Expose Odoo static analysis — diagnostics, hover, go-to-definition, completions — as
MCP tools callable by Claude, Cursor, Copilot, and other MCP-aware agents.

## Requirements

- Python 3.11+
- [`odoo_ls_server`](https://github.com/odoo/odoo-ls) installed and on `PATH`
- An `odools.toml` config in your workspace

## Installation

```bash
uv pip install odoo-ls-mcp
# or from source:
git clone https://github.com/your-org/odoo-ls-mcp
cd odoo-ls-mcp
uv sync
```

## Usage

### As an MCP server (stdio)

Add to your MCP client config (e.g. `~/.config/opencode/config.json`):

```json
{
  "mcpServers": {
    "odoo-ls": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/odoo-ls-mcp", "odoo-ls-mcp"]
    }
  }
}
```

### Available Tools

| Tool | Description |
|------|-------------|
| `check_odools_available` | Verify OdooLS is installed and return version info |
| `list_odools_config` | Find and preview `odools.toml` files from a path |
| `parse_diagnostics` | Run one-shot static analysis on an Odoo workspace |

### Example: run diagnostics on a workspace

```
check_odools_available()
parse_diagnostics(workspace="/path/to/my-odoo-project", min_severity=2)
```

## Architecture

### v1 (current) — `--parse` mode

```
Agent (Claude/Cursor)
  └─ MCP stdio transport
       └─ odoo-ls-mcp FastMCP server
            └─ asyncio subprocess: odoo_ls_server --parse <workspace>
                 └─ JSON diagnostics → parsed → returned as text
```

No long-lived process. Each `parse_diagnostics` call spawns and waits for
`odoo_ls_server`, then exits.

### v1.1 (planned) — live LSP session

Long-lived `odoo_ls_server` subprocess with full LSP initialize handshake,
enabling hover, go-to-definition, completions, and live diagnostics.

## Development

```bash
uv sync
uv run pytest
uv run ruff check src tests
```

## Notes

- **stdout is never used** by this server — all logging goes to stderr.
  This is required for stdio MCP transport correctness.
- `start_new_session=True` prevents signal propagation to OdooLS child processes.
- `stdin=DEVNULL` prevents the child from inheriting the MCP stdin stream.
