from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from odoo_ls_mcp.server import (
    inspect_workspace_config,
    lookup_model,
    lookup_xmlid,
    session_health,
)


# ── session_health ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_health_no_sessions():
    with patch("odoo_ls_mcp.server.get_registry") as mock_reg:
        mock_reg.return_value.status.return_value = {}
        result = await session_health()
    assert "No active" in result
    assert "start_session" in result


@pytest.mark.asyncio
async def test_session_health_with_ready_session():
    mock_session = MagicMock()
    mock_session.is_ready = True
    mock_session._progress = {}

    with patch("odoo_ls_mcp.server.get_registry") as mock_reg:
        mock_reg.return_value.status.return_value = {("/some/workspace", None): "READY"}
        mock_reg.return_value.get = AsyncMock(return_value=mock_session)
        result = await session_health()

    assert "READY" in result
    assert "/some/workspace" in result
    assert "✅" in result


@pytest.mark.asyncio
async def test_session_health_with_indexing_session():
    prog = MagicMock()
    prog.done = False
    prog.title = "Indexing"
    prog.message = "files..."
    prog.percentage = 42

    mock_session = MagicMock()
    mock_session.is_ready = False
    mock_session._progress = {"tok1": prog}

    with patch("odoo_ls_mcp.server.get_registry") as mock_reg:
        mock_reg.return_value.status.return_value = {("/ws", None): "INDEXING"}
        mock_reg.return_value.get = AsyncMock(return_value=mock_session)
        result = await session_health()

    assert "INDEXING" in result
    assert "42%" in result


# ── inspect_workspace_config ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inspect_workspace_config_not_found(tmp_path):
    result = await inspect_workspace_config(
        workspace=str(tmp_path / "nonexistent"),
    )
    assert "❌" in result


@pytest.mark.asyncio
async def test_inspect_workspace_config_no_toml(tmp_path):
    fake_bin = tmp_path / "odoo_ls_server"
    fake_bin.touch()
    fake_bin.chmod(0o755)
    with patch("odoo_ls_mcp.config._resolve_binary", return_value=fake_bin):
        result = await inspect_workspace_config(workspace=str(tmp_path))
    assert "❌" in result or "Workspace root" in result


@pytest.mark.asyncio
async def test_inspect_workspace_config_with_toml(tmp_path):
    toml_file = tmp_path / "odools.toml"
    toml_file.write_text('[[config]]\nname = "test"\n')
    fake_bin = tmp_path / "odoo_ls_server"
    fake_bin.touch()
    fake_bin.chmod(0o755)
    with patch("odoo_ls_mcp.config._resolve_binary", return_value=fake_bin):
        result = await inspect_workspace_config(workspace=str(tmp_path))
    assert "odools.toml" in result or "Config" in result
    assert "test" in result


# ── lookup_model ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lookup_model_workspace_not_found(tmp_path):
    result = await lookup_model(
        workspace=str(tmp_path / "nonexistent"),
        model="sale.order",
    )
    assert "❌" in result
    assert "not exist" in result


@pytest.mark.asyncio
async def test_lookup_model_no_matches(tmp_path):
    with patch(
        "odoo_ls_mcp.server._lookup_model_via_odools", new=AsyncMock()
    ) as mock_fn:
        mock_fn.return_value = (
            f"No model definitions found matching 'no.such.model' in {tmp_path}."
        )
        result = await lookup_model(workspace=str(tmp_path), model="no.such.model")
    assert "No model definitions found" in result
    assert "no.such.model" in result


@pytest.mark.asyncio
async def test_lookup_model_finds_name(tmp_path):
    with patch(
        "odoo_ls_mcp.server._lookup_model_via_odools", new=AsyncMock()
    ) as mock_fn:
        mock_fn.return_value = (
            "🔍 Model lookup: 'sale.order'  —  1 result(s)\n\n  📄 model.py:2"
        )
        result = await lookup_model(workspace=str(tmp_path), model="sale.order")
    assert "sale.order" in result
    assert "model.py" in result


@pytest.mark.asyncio
async def test_lookup_model_finds_inherit(tmp_path):
    with patch(
        "odoo_ls_mcp.server._lookup_model_via_odools", new=AsyncMock()
    ) as mock_fn:
        mock_fn.return_value = (
            "🔍 Model lookup: 'sale.order'  —  1 result(s)\n\n  📄 ext.py:2"
        )
        result = await lookup_model(workspace=str(tmp_path), model="sale.order")
    assert "sale.order" in result
    assert "ext.py" in result


@pytest.mark.asyncio
async def test_lookup_model_timeout(tmp_path):
    with patch(
        "odoo_ls_mcp.server._lookup_model_via_odools",
        new=AsyncMock(side_effect=TimeoutError()),
    ):
        result = await lookup_model(workspace=str(tmp_path), model="sale.order")
    assert "timed out" in result.lower()


# ── lookup_xmlid ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lookup_xmlid_workspace_not_found(tmp_path):
    result = await lookup_xmlid(
        workspace=str(tmp_path / "nonexistent"),
        xmlid="sale.action_orders",
    )
    assert "❌" in result
    assert "not exist" in result


@pytest.mark.asyncio
async def test_lookup_xmlid_no_matches(tmp_path):
    result = await lookup_xmlid(workspace=str(tmp_path), xmlid="no_such_xmlid_xyz")
    assert "No XML ID" in result
    assert "no_such_xmlid_xyz" in result


@pytest.mark.asyncio
async def test_lookup_xmlid_finds_xml_id(tmp_path):
    xml_file = tmp_path / "data.xml"
    xml_file.write_text('<record id="action_orders" model="ir.actions.act_window"/>\n')
    result = await lookup_xmlid(workspace=str(tmp_path), xmlid="action_orders")
    assert "action_orders" in result
    assert "data.xml" in result


@pytest.mark.asyncio
async def test_lookup_xmlid_strips_module_prefix(tmp_path):
    xml_file = tmp_path / "data.xml"
    xml_file.write_text('<record id="action_orders" model="ir.actions.act_window"/>\n')
    result = await lookup_xmlid(workspace=str(tmp_path), xmlid="sale.action_orders")
    assert "action_orders" in result


@pytest.mark.asyncio
async def test_lookup_xmlid_timeout(tmp_path):
    with patch(
        "odoo_ls_mcp.server._run_subprocess",
        new=AsyncMock(side_effect=__import__("subprocess").TimeoutExpired("grep", 15)),
    ):
        result = await lookup_xmlid(workspace=str(tmp_path), xmlid="some.xmlid")
    assert "timed out" in result.lower()
