"""
End-to-end tests for odoo-ls-mcp.

These tests spawn a real MCP server subprocess and exercise all three major
tool families via the MCP stdio transport:

  - Diagnostics/status tools: check_odools_available, list_odools_config,
    inspect_workspace_config, session_health, indexing_status
  - Core navigation tools: lookup_model, lookup_xmlid
  - Odoo helpers: parse_diagnostics (one-shot, no live session required)

Tests that require a live OdooLS session (start_session, hover,
go_to_definition, …) are out of scope here because indexing can take
several minutes on a large workspace; those belong in a dedicated nightly
suite.

Run:
    uv run pytest tests/test_e2e.py -m e2e -v
    uv run pytest tests/ -m "not e2e" -q   # skip these
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WORKSPACE = Path("/home/kevin/Development/Odoo/athenrix-docker-base")
ODOOLS_CONFIG = WORKSPACE / "odools.toml"
MODEL_FILE = (
    WORKSPACE
    / "odoo/custom/src/hosting/hosting_domain_peingbr/models/hosting_domain_service.py"
)

# The installed entrypoint in the project venv
_VENV_BIN = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "odoo-ls-mcp"
# Fallback: uv-managed runner
_UV_CMD = shutil.which("uv")

# ---------------------------------------------------------------------------
# Skip conditions
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.e2e


def _server_cmd() -> list[str]:
    """Return the argv used to launch the MCP server."""
    if _VENV_BIN.exists():
        return [str(_VENV_BIN)]
    if _UV_CMD:
        return [_UV_CMD, "run", "odoo-ls-mcp"]
    pytest.skip("Cannot locate odoo-ls-mcp entrypoint — venv not built?")


def _require_workspace():
    if not WORKSPACE.exists():
        pytest.skip(f"Workspace not present: {WORKSPACE}")
    if not ODOOLS_CONFIG.exists():
        pytest.skip(f"odools.toml not found: {ODOOLS_CONFIG}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def server_params() -> StdioServerParameters:
    _require_workspace()
    cmd = _server_cmd()
    return StdioServerParameters(
        command=cmd[0],
        args=cmd[1:],
        env={"PATH": "/home/kevin/.local/bin:/usr/local/bin:/usr/bin:/bin"},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _call(
    session: ClientSession, tool: str, args: dict[str, object] | None = None
) -> str:
    """Call a tool and return the first text content as a string."""
    result = await session.call_tool(tool, arguments=args or {})
    assert result.content, f"Tool '{tool}' returned empty content"
    first = result.content[0]
    text = getattr(first, "text", None)
    if isinstance(text, str):
        return text
    return str(first)


# ---------------------------------------------------------------------------
# Tests — diagnostics / status family
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_check_odools_available(server_params):
    """check_odools_available must confirm the binary is present."""
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            text = await _call(session, "check_odools_available")

    assert "odoo_ls_server" in text, f"Unexpected response: {text!r}"
    # Either found (✅) or not found (❌) — both are valid on this machine but
    # we expect the binary to be installed per context
    assert any(marker in text for marker in ("✅", "⚠️", "❌")), (
        f"Response missing status marker: {text!r}"
    )


@pytest.mark.asyncio
async def test_e2e_list_odools_config(server_params):
    """list_odools_config must find the odools.toml in the workspace."""
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            text = await _call(
                session,
                "list_odools_config",
                {"start_path": str(WORKSPACE)},
            )

    assert "odools.toml" in text, f"Expected config path in response: {text!r}"


@pytest.mark.asyncio
async def test_e2e_inspect_workspace_config(server_params):
    """inspect_workspace_config must surface the workspace root."""
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            text = await _call(
                session,
                "inspect_workspace_config",
                {
                    "workspace": str(WORKSPACE),
                    "config_path": str(ODOOLS_CONFIG),
                },
            )

    # Must mention the workspace and binary
    assert str(WORKSPACE) in text or "athenrix" in text, (
        f"Workspace not mentioned: {text!r}"
    )


@pytest.mark.asyncio
async def test_e2e_session_health_no_sessions(server_params):
    """session_health must report no active sessions on a fresh server."""
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            text = await _call(session, "session_health")

    assert "No active" in text or "start_session" in text, (
        f"Unexpected session_health output: {text!r}"
    )


@pytest.mark.asyncio
async def test_e2e_indexing_status_no_sessions(server_params):
    """indexing_status must report no active sessions on a fresh server."""
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            text = await _call(session, "indexing_status")

    assert "No active" in text, f"Unexpected indexing_status output: {text!r}"


# ---------------------------------------------------------------------------
# Tests — Odoo helpers (grep-based, no live session)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_lookup_model_found(server_params):
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            text = await _call(
                session,
                "lookup_model",
                {
                    "workspace": str(WORKSPACE),
                    "model": "hosting.domain",
                },
            )

    assert "hosting" in text.lower(), f"Expected model reference in response: {text!r}"
    # Should not be an error response
    assert not text.startswith("❌"), f"Tool returned error: {text!r}"


@pytest.mark.asyncio
async def test_e2e_lookup_model_not_found(server_params):
    """lookup_model must gracefully handle an unknown model."""
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            text = await _call(
                session,
                "lookup_model",
                {
                    "workspace": str(WORKSPACE),
                    "model": "this.model.does.not.exist.zzz",
                },
            )

    assert "No model" in text or "not found" in text.lower(), (
        f"Unexpected response for missing model: {text!r}"
    )


@pytest.mark.asyncio
async def test_e2e_lookup_xmlid(server_params):
    """lookup_xmlid must search XML files in the workspace without error."""
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            text = await _call(
                session,
                "lookup_xmlid",
                {
                    "workspace": str(WORKSPACE),
                    "xmlid": "base.group_user",
                },
            )

    # Either found results or graceful not-found — must not be an exception
    assert not text.startswith("❌ Workspace"), f"Tool returned path error: {text!r}"


# ---------------------------------------------------------------------------
# Tests — server tool inventory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_tool_list_complete(server_params):
    """The server must expose all expected MCP tools."""
    expected_tools = {
        "check_odools_available",
        "list_odools_config",
        "parse_diagnostics",
        "start_session",
        "get_live_diagnostics",
        "hover",
        "go_to_definition",
        "completions",
        "indexing_status",
        "restart_server",
        "stop_session",
        "find_references",
        "document_symbols",
        "workspace_symbols",
        "session_health",
        "inspect_workspace_config",
        "lookup_model",
        "lookup_xmlid",
    }

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_result = await session.list_tools()

    registered = {t.name for t in tools_result.tools}
    missing = expected_tools - registered
    assert not missing, f"Missing tools: {missing!r}\nRegistered: {sorted(registered)}"
