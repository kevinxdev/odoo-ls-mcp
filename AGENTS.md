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

## Repository Structure

```
odoo-ls-mcp/
├── src/odoo_ls_mcp/
│   ├── __init__.py          — Package entry point
│   ├── __main__.py          — CLI entry (delegates to server.py)
│   ├── server.py            — FastMCP server definition + tool endpoints
│   ├── models.py            — Shared Pydantic data models
│   ├── parse_tool.py        — OdooLS --parse subprocess orchestration
│   ├── config.py            — Configuration and workspace resolution
│   ├── lsp_client.py        — Low-level LSP JSON-RPC client
│   ├── session.py           — Abstract session management
│   ├── lsp_session.py       — Long-lived LSP session implementation
│   ├── sync.py              — File synchronization (didOpen/didChange)
│   ├── diagnostics_cache.py — Caching for pushed diagnostics
│   └── utils.py             — Shared utilities (URI conversion, etc.)
├── tests/
│   ├── test_parse_tool.py
│   ├── test_config.py
│   ├── test_session.py
│   ├── test_sync.py
│   ├── test_integration.py
│   ├── test_lsp_session.py
│   ├── test_navigation_tools.py
│   ├── test_odoo_helpers.py
│   ├── test_lifecycle_hardening.py
│   └── test_e2e.py          — Full MCP-to-LSP end-to-end tests
├── docs/
│   └── opencode-integration.md — detailed setup guide
├── pyproject.toml           — uv-managed project config (Python 3.11+)
├── README.md                — User-facing documentation
└── uv.lock                  — Locked dependency versions
```

## Installation & Setup

### Prerequisites

- **Python 3.11+**
- **`odoo_ls_server`** installed and on `PATH` (or `ODOO_LS_PATH` set)
- **An `odools.toml`** config file in your workspace

### Local Development

```bash
# Clone and sync dependencies
cd ~/Development/odoo-ls-mcp
uv sync

# Verify installation
uv run odoo-ls-mcp --help
```

## Running the Server

### As MCP stdio server

```bash
uv run odoo-ls-mcp
```

The server binds to **stdin/stdout** and logs all output to **stderr**. This is
required for MCP stdio transport correctness.

## Available Tools (10+)

| Tool | Purpose |
|------|---------|
| `check_odools_available` | Verify OdooLS environment. |
| `list_odools_config` | Find upward `odools.toml` files. |
| `inspect_workspace_config` | Display resolved settings. |
| `parse_diagnostics` | One-shot static analysis. |
| `start_session` | Start live LSP session (required for navigation). |
| `hover` | Get type/docs at position. |
| `go_to_definition` | Jump to symbol definition. |
| `find_references` | Find symbol usages. |
| `document_symbols` | List symbols in a specific file. |
| `workspace_symbols` | Search symbols across workspace. |
| `lookup_model` | Fast model finder. |
| `lookup_xmlid` | Fast XML ID finder. |
| `session_health` | Report health of active sessions. |

## Development Conventions

### Logging

**Rule**: All logging goes to stderr; **never** use print() or log to stdout.

### FastMCP Tool Definition

Tools are registered via the `@mcp.tool()` decorator. Every tool must have a descriptive docstring and Annotated parameters with Field descriptions for the LLM to understand how to use it.

### Testing

The test suite is categorized by markers:

```bash
# All tests
uv run pytest tests/ -q

# Skip slow E2E tests
uv run pytest tests/ -m "not e2e" -q

# E2E tests only
uv run pytest tests/ -m e2e -q
```

## Troubleshooting

### ConfigError
Usually means no `odools.toml` was found. Ensure one exists in the workspace root.

### LSP Handshake Failures
Verify `odoo_ls_server --version` works and that no other process is conflicting with the spawned server.
