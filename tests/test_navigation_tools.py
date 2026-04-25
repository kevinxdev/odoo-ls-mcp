"""
Unit tests for the navigation MCP tools: find_references, document_symbols,
workspace_symbols, and the updated go_to_definition / hover.

All tests mock LspSession — no real OdooLS process is needed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from odoo_ls_mcp import server
from odoo_ls_mcp.lsp_session import LspSession, SessionState


def make_ready_session(workspace: Path) -> MagicMock:
    session = MagicMock(spec=LspSession)
    type(session).is_ready = PropertyMock(return_value=True)
    type(session).state = PropertyMock(return_value=SessionState.READY)
    session.workspace = workspace
    session.find_references = AsyncMock(return_value=[])
    session.document_symbols = AsyncMock(return_value=[])
    session.workspace_symbols = AsyncMock(return_value=[])
    session.go_to_definition = AsyncMock(return_value=[])
    session.hover = AsyncMock(return_value=None)
    return session


def _registry_patch(session):
    registry = MagicMock()
    registry.get = AsyncMock(return_value=session)
    return patch("odoo_ls_mcp.server.get_registry", return_value=registry)


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture()
def src_file(tmp_path: Path) -> Path:
    f = tmp_path / "model.py"
    f.write_text("class Foo:\n    pass\n")
    return f


@pytest.mark.asyncio
async def test_find_references_no_session(ws: Path, src_file: Path):
    registry = MagicMock()
    registry.get = AsyncMock(return_value=None)
    with patch("odoo_ls_mcp.server.get_registry", return_value=registry):
        result = await server.find_references(str(ws), str(src_file), 0, 0)
    assert "start_session" in result


@pytest.mark.asyncio
async def test_find_references_file_not_found(ws: Path):
    session = make_ready_session(ws)
    with _registry_patch(session):
        result = await server.find_references(str(ws), str(ws / "missing.py"), 0, 0)
    assert "not found" in result.lower()


@pytest.mark.asyncio
async def test_find_references_empty(ws: Path, src_file: Path):
    session = make_ready_session(ws)
    session.find_references = AsyncMock(return_value=[])
    with _registry_patch(session):
        result = await server.find_references(str(ws), str(src_file), 0, 0)
    assert "No references" in result


@pytest.mark.asyncio
async def test_find_references_returns_locations(ws: Path, src_file: Path):
    session = make_ready_session(ws)
    session.find_references = AsyncMock(
        return_value=[
            {
                "uri": f"file://{src_file}",
                "range": {
                    "start": {"line": 0, "character": 6},
                    "end": {"line": 0, "character": 9},
                },
            },
            {
                "uri": f"file://{src_file}",
                "range": {
                    "start": {"line": 5, "character": 0},
                    "end": {"line": 5, "character": 3},
                },
            },
        ]
    )
    with _registry_patch(session):
        result = await server.find_references(str(ws), str(src_file), 0, 6)
    assert "2 location(s)" in result
    assert str(src_file) in result


@pytest.mark.asyncio
async def test_find_references_timeout(ws: Path, src_file: Path):
    session = make_ready_session(ws)
    session.find_references = AsyncMock(side_effect=TimeoutError("too slow"))
    with _registry_patch(session):
        result = await server.find_references(str(ws), str(src_file), 0, 0)
    assert "timed out" in result.lower()


@pytest.mark.asyncio
async def test_find_references_lsp_error(ws: Path, src_file: Path):
    session = make_ready_session(ws)
    session.find_references = AsyncMock(side_effect=RuntimeError("LSP error -32601"))
    with _registry_patch(session):
        result = await server.find_references(str(ws), str(src_file), 0, 0)
    assert "LSP error" in result


@pytest.mark.asyncio
async def test_document_symbols_no_session(ws: Path, src_file: Path):
    registry = MagicMock()
    registry.get = AsyncMock(return_value=None)
    with patch("odoo_ls_mcp.server.get_registry", return_value=registry):
        result = await server.document_symbols(str(ws), str(src_file))
    assert "start_session" in result


@pytest.mark.asyncio
async def test_document_symbols_file_not_found(ws: Path):
    session = make_ready_session(ws)
    with _registry_patch(session):
        result = await server.document_symbols(str(ws), str(ws / "missing.py"))
    assert "not found" in result.lower()


@pytest.mark.asyncio
async def test_document_symbols_empty(ws: Path, src_file: Path):
    session = make_ready_session(ws)
    session.document_symbols = AsyncMock(return_value=[])
    with _registry_patch(session):
        result = await server.document_symbols(str(ws), str(src_file))
    assert "No symbols" in result


@pytest.mark.asyncio
async def test_document_symbols_returns_list(ws: Path, src_file: Path):
    session = make_ready_session(ws)
    session.document_symbols = AsyncMock(
        return_value=[
            {
                "name": "Foo",
                "kind": 5,
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 1, "character": 8},
                },
                "selectionRange": {
                    "start": {"line": 0, "character": 6},
                    "end": {"line": 0, "character": 9},
                },
                "uri": f"file://{src_file}",
            },
            {
                "name": "bar",
                "kind": 6,
                "containerName": "Foo",
                "range": {
                    "start": {"line": 1, "character": 4},
                    "end": {"line": 1, "character": 7},
                },
                "uri": f"file://{src_file}",
            },
        ]
    )
    with _registry_patch(session):
        result = await server.document_symbols(str(ws), str(src_file))
    assert "2 symbol(s)" in result
    assert "Foo" in result
    assert "Class" in result


@pytest.mark.asyncio
async def test_document_symbols_timeout(ws: Path, src_file: Path):
    session = make_ready_session(ws)
    session.document_symbols = AsyncMock(side_effect=TimeoutError("too slow"))
    with _registry_patch(session):
        result = await server.document_symbols(str(ws), str(src_file))
    assert "timed out" in result.lower()


@pytest.mark.asyncio
async def test_workspace_symbols_no_session(ws: Path):
    registry = MagicMock()
    registry.get = AsyncMock(return_value=None)
    with patch("odoo_ls_mcp.server.get_registry", return_value=registry):
        result = await server.workspace_symbols(str(ws), "SaleOrder")
    assert "start_session" in result


@pytest.mark.asyncio
async def test_workspace_symbols_empty(ws: Path):
    session = make_ready_session(ws)
    session.workspace_symbols = AsyncMock(return_value=[])
    with _registry_patch(session):
        result = await server.workspace_symbols(str(ws), "NonExistent")
    assert "No symbols" in result


@pytest.mark.asyncio
async def test_workspace_symbols_returns_list(ws: Path, src_file: Path):
    session = make_ready_session(ws)
    session.workspace_symbols = AsyncMock(
        return_value=[
            {
                "name": "SaleOrder",
                "kind": 5,
                "containerName": "odoo.addons.sale.models",
                "location": {
                    "uri": f"file://{src_file}",
                    "range": {
                        "start": {"line": 10, "character": 0},
                        "end": {"line": 50, "character": 0},
                    },
                },
            }
        ]
    )
    with _registry_patch(session):
        result = await server.workspace_symbols(str(ws), "SaleOrder")
    assert "1 symbol(s)" in result
    assert "SaleOrder" in result
    assert "Class" in result


@pytest.mark.asyncio
async def test_workspace_symbols_timeout(ws: Path):
    session = make_ready_session(ws)
    session.workspace_symbols = AsyncMock(side_effect=TimeoutError("too slow"))
    with _registry_patch(session):
        result = await server.workspace_symbols(str(ws), "Foo")
    assert "timed out" in result.lower()


@pytest.mark.asyncio
async def test_workspace_symbols_lsp_error(ws: Path):
    session = make_ready_session(ws)
    session.workspace_symbols = AsyncMock(
        side_effect=RuntimeError("method not supported")
    )
    with _registry_patch(session):
        result = await server.workspace_symbols(str(ws), "Foo")
    assert "LSP error" in result
