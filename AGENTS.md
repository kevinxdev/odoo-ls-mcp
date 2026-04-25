# AGENTS.md — Development Guide for odoo-ls-mcp

This document provides essential context for agents and developers working with the
**odoo-ls-mcp** repository.

## Repository Overview

`odoo-ls-mcp` is a **Model Context Protocol (MCP) server** that bridges
[OdooLS](https://github.com/odoo/odoo-ls) (Odoo Language Server) to LLM agents via
the [Model Context Protocol](https://modelcontextprotocol.io).

### Purpose

Expose Odoo static analysis capabilities — diagnostics, hover, go-to-definition,
completions — as MCP tools callable by Claude, Cursor, Copilot, and other MCP-aware
agents.

### Current Scope (v0.1.0)

- **v1 (current)**: One-shot `--parse` mode via `odoo_ls_server --parse <workspace>`
- **v1.1 (planned)**: Long-lived LSP session with hover, go-to-definition, completions

## Repository Structure

```
odoo-ls-mcp/
├── src/odoo_ls_mcp/
│   ├── __init__.py          — Package entry point
│   ├── __main__.py          — CLI entry (delegates to server.py)
│   ├── server.py            — FastMCP server definition + tool endpoints
│   ├── parse_tool.py        — OdooLS --parse subprocess orchestration
│   └── models.py            — Shared Pydantic data models
├── tests/
│   ├── __init__.py
│   └── test_parse_tool.py   — Unit tests for parse_tool module
├── pyproject.toml           — uv-managed project config (Python 3.11+)
├── README.md                — User-facing documentation
└── uv.lock                  — Locked dependency versions
```

## Installation & Setup

### Prerequisites

- **Python 3.11+** (as per `pyproject.toml`)
- **`odoo_ls_server`** installed and on `PATH`
- **An `odools.toml`** config file in your workspace (required by OdooLS)

### Local Development

```bash
# Clone and sync dependencies
git clone https://github.com/your-org/odoo-ls-mcp
cd odoo-ls-mcp
uv sync

# Verify installation (--help exits 0)
uv run odoo-ls-mcp --help
```

## Running the Server

### As MCP stdio server (production)

```bash
uv run odoo-ls-mcp
```

The server binds to **stdin/stdout** and logs all output to **stderr**. This is
required for MCP stdio transport correctness.

Register in your MCP client config (e.g., `~/.config/opencode/config.json`):

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

## Available Tools

Three tools are currently exposed via MCP:

| Tool | Purpose | Requires `odoo_ls_server` |
|------|---------|--------------------------|
| `check_odools_available` | Verify OdooLS install + version info | Yes |
| `list_odools_config` | Find + preview `odools.toml` files from a path | No |
| `parse_diagnostics` | One-shot static analysis of an Odoo workspace | Yes |

### Example Usage

```python
# 1. Check OdooLS is available
check_odools_available()

# 2. List configs from a workspace
list_odools_config(path="/path/to/my-odoo-project")

# 3. Run diagnostics (requires odools.toml in workspace)
parse_diagnostics(
    workspace="/path/to/my-odoo-project",
    config_path=None,  # Optional, walks up from workspace if omitted
    timeout=120,       # Seconds
    min_severity=2     # 1=error, 2=warning+, 3=info+, 4=hints
)
```

## Key Modules

### `server.py`

**FastMCP server definition.**

- Initializes `FastMCP` instance with name `"odoo-ls-mcp"`
- Registers three tool endpoints (functions decorated with `@mcp.tool()`)
- Configures logging to stderr only (stdout reserved for MCP transport)
- Entry point: `main()` function

**Key detail**: All logging uses Python's stdlib `logging` module, directed to stderr.
**Never print to stdout** — this breaks MCP stdio protocol.

### `parse_tool.py`

**OdooLS --parse mode orchestration.**

Core functions:

- `_find_binary()` — Locate `odoo_ls_server` on PATH via `shutil.which()`
- `_build_command()` — Construct subprocess command with workspace + optional config
- `run_parse()` — **Main entry**: spawn `odoo_ls_server --parse`, collect JSON output,
  parse + filter diagnostics
- `format_diagnostics_text()` — Convert parsed diagnostics to human-readable text
- `parse_lsp_diagnostics()` — Parse JSON diagnostics from OdooLS stdout

**Important design choices**:

1. **One-shot subprocess**: Each `parse_diagnostics` call creates a fresh subprocess.
   No long-lived LSP session (planned for v1.1).
2. **Timeout handling**: Default 120s; large workspaces may need up to 300s.
3. **stdin=DEVNULL**: Prevents child process from inheriting MCP stdin stream.
4. **start_new_session=True**: Prevents signal propagation (SIGINT, SIGTERM) from
   killing the OdooLS process unexpectedly.
5. **Severity filtering**: Diagnostic entries are filtered by `min_severity` before
   returning to the caller.

### `models.py`

**Shared Pydantic data models.**

Classes:

- `DiagnosticSeverity(IntEnum)` — Severity levels: ERROR=1, WARNING=2, INFO=3, HINT=4
- `Position` — Line + character in a file
- `Range` — Start + end positions
- `Diagnostic` — Parsed diagnostic entry (file, range, severity, message, source)
- `ParseResult` — Container for diagnostics from a single `--parse` run

All models use Pydantic v2 for validation and serialization.

## Testing

### Run Tests

```bash
# All tests
uv run pytest

# Verbose output
uv run pytest -v

# Single test file
uv run pytest tests/test_parse_tool.py

# Specific test
uv run pytest tests/test_parse_tool.py::test_run_parse_success
```

### Test Coverage

**File**: `tests/test_parse_tool.py` (13 tests)

Tests cover:

- Diagnostic parsing (basic, empty, malformed)
- URI-to-path conversion
- Text formatting
- Binary lookup (not found, found)
- Workspace validation
- Subprocess invocation + timeout
- Severity filtering
- Error handling (nonzero exit codes)

All tests pass with Python 3.11 + pytest-asyncio.

## Development Conventions

### Logging

**Rule**: All logging goes to stderr; **never** use print() or log to stdout.

```python
import logging
import sys

logger = logging.getLogger(__name__)

# This is configured at server startup:
logging.basicConfig(
    stream=sys.stderr,
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# Use it like normal
logger.warning("This goes to stderr, MCP stays on stdout ✓")
```

### FastMCP Tool Definition

Tools are registered via the `@mcp.tool()` decorator. Every tool must have:

1. **Name** — Kebab-case identifier (e.g., `parse_diagnostics`)
2. **Description** — Multi-sentence explanation of what it does
3. **Annotated parameters** — Each param has a `Field()` with a description
4. **Return value** — String (formatted text) or structured dict

Example:

```python
@mcp.tool(
    name="parse_diagnostics",
    description="Run one-shot static analysis of an Odoo workspace..."
)
async def parse_diagnostics(
    workspace: Annotated[
        str,
        Field(description="Absolute path to Odoo workspace root...")
    ],
) -> str:
    # Perform work, return text
    return formatted_results
```

### Async/Await

The server uses `asyncio` for concurrency. All tool functions must be `async` and
use `await` for I/O (subprocess, file operations, etc.).

### Type Hints

Use `from __future__ import annotations` + full type hints (Python 3.11+):

```python
from typing import Annotated
from pydantic import Field

async def my_tool(
    param1: Annotated[str, Field(description="...")],
    param2: int = 5,
) -> str:
    ...
```

## Code Style & Linting

### Ruff Configuration

See `pyproject.toml`:

```toml
[tool.ruff]
target-version = "py311"
line-length = 88

[tool.ruff.lint]
select = ["E", "F", "B", "I", "UP"]
ignore = ["E501"]  # Line length ignored (handled separately)
```

**Checks enabled**:
- E/W — pycodestyle errors/warnings
- F — Pyflakes (undefined names, unused imports)
- B — flake8-bugbear
- I — isort (import ordering)
- UP — pyupgrade (modern Python syntax)

### Lint Check

```bash
uv run ruff check src tests
```

## Git & Commits

This repo follows **conventional commits**:

```
feat(parse): add support for custom severity thresholds
fix(server): prevent stdout pollution in error handler
docs: update README with v1.1 roadmap
```

## Debugging

### Enable Debug Logging

Temporarily modify `server.py`:

```python
logging.basicConfig(
    stream=sys.stderr,
    level=logging.DEBUG,  # Instead of logging.WARNING
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
```

Then restart the server to see DEBUG-level logs.

### Inspect OdooLS Command

Insert a print to stderr in `parse_tool.py`:

```python
def _build_command(...) -> list[str]:
    cmd = [binary, "--parse", str(workspace)]
    # ... 
    logger.debug(f"OdooLS command: {cmd}")
    return cmd
```

### Test Individual Tools

Create a quick test script:

```python
import asyncio
from src.odoo_ls_mcp.server import check_odools_available

async def main():
    result = await check_odools_available()
    print(result)

asyncio.run(main())
```

## Future Roadmap (v1.1+)

- **Long-lived LSP session**: Instead of one-shot `--parse`, maintain a live
  `odoo_ls_server` subprocess with LSP initialize/shutdown handshake.
- **Hover + go-to-definition**: Expose `textDocument/hover`, `textDocument/definition`.
- **Completions**: Expose `textDocument/completion`.
- **Real-time diagnostics**: Push diagnostics as they are computed, not at end.

## CI/CD & Quality

### Pre-commit Hooks

If you add pre-commit hooks, register them in `.pre-commit-config.yaml` (currently
not present; add if needed).

### GitHub Actions

If you set up CI, recommended workflow:

```yaml
- Run: uv run pytest
- Run: uv run ruff check src tests
```

## Troubleshooting

### Error: `'odoo_ls_server' not found on PATH`

**Cause**: OdooLS is not installed or not on PATH.

**Solution**:
1. Install OdooLS: `pip install odoo_ls_server` or per OdooLS docs
2. Verify: `which odoo_ls_server` returns a path
3. Retry `parse_diagnostics`

### Error: `workspace must exist and contain or be covered by odools.toml`

**Cause**: The specified workspace path doesn't exist, or no `odools.toml` is
discoverable from it.

**Solution**:
1. Verify the workspace path exists: `ls -d /path/to/workspace`
2. Create an `odools.toml` in the workspace or a parent directory
3. See [OdooLS docs](https://github.com/odoo/odoo-ls) for `odools.toml` format

### Timeout during `parse_diagnostics`

**Cause**: Large workspaces or slow I/O take longer than the default 120 seconds.

**Solution**: Increase the `timeout` parameter:

```python
parse_diagnostics(workspace="/path", timeout=300)  # 5 minutes
```

### MCP server exits immediately

**Cause**: An exception occurred during `main()`.

**Solution**: Check stderr for traceback. Enable DEBUG logging in `server.py`.

## Contact & Support

For issues or feature requests, open an issue in the repository.
