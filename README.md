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
# Clone the repository
git clone https://github.com/your-org/odoo-ls-mcp
cd odoo-ls-mcp

# Install dependencies and create virtual environment
uv sync
```

## OpenCode Registration

To use this server with OpenCode, add the following to your `~/.config/opencode/opencode.json`:

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

See [docs/opencode-integration.md](docs/opencode-integration.md) for a detailed integration guide.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `ODOO_LS_PATH` | Path to the `odoo_ls_server` binary. |

## Workspace Configuration

Ensure an `odools.toml` exists in your workspace root.

```toml
[[config]]
odoo_path = "/path/to/odoo/source"
addons_paths = ["$autoDetectAddons"]
```

## Tool Reference

| Tool | Description |
|------|-------------|
| `check_odools_available` | Verify OdooLS environment. |
| `list_odools_config` | Find upward `odools.toml` files. |
| `inspect_workspace_config` | Display resolved settings. |
| `parse_diagnostics` | One-shot static analysis. |
| `start_session` | Start live LSP session (for navigation). |
| `hover` | Get hover info at position. |
| `go_to_definition` | Jump to symbol definition. |
| `find_references` | Find symbol usages. |
| `lookup_model` | Fast model finder. |
| `lookup_xmlid` | Fast XML ID finder. |

## Troubleshooting

- Verify `ODOO_LS_PATH` points to the correct binary.
- Check that the `.venv` path in your MCP config is absolute and correct.
- Ensure your `odools.toml` has a valid `odoo_path`.

## Scope Limits

- Navigation and diagnostics only.
- No renaming or code actions.
- No formatting support.

## Development

```bash
uv sync
uv run pytest
uv run ruff check src tests
```

## Testing

The test suite uses three pytest markers:

| Marker | Scope | Speed |
|--------|-------|-------|
| `unit` | Pure unit tests, no external processes | Fast (< 1 s each) |
| `integration` | Requires `odoo_ls_server` binary on PATH | Medium |
| `e2e` | Spawns a real MCP server subprocess | Slow (9–30 s per test) |

**Run all tests (unit + integration + e2e — default):**
```bash
uv run pytest tests/ -q
```

**Run everything except e2e:**
```bash
uv run pytest tests/ -m "not e2e" -q
```

**Run e2e tests only:**
```bash
uv run pytest tests/ -m e2e -q
```

End-to-end tests require `odoo_ls_server` on PATH and the Odoo workspace at
`/home/kevin/Development/Odoo/athenrix-docker-base` with a valid `odools.toml`.

## Notes

- **stdout is never used** by this server — all logging goes to stderr.
  This is required for stdio MCP transport correctness.
- `start_new_session=True` prevents signal propagation to OdooLS child processes.
- `stdin=DEVNULL` prevents the child from inheriting the MCP stdin stream.
