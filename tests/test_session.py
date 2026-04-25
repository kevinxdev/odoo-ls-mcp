"""
Tests for OdooLsSessionManager (session.py) and LspClient (lsp_client.py).

All tests mock LspClient.start / stop / is_alive — no real OdooLS process needed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from odoo_ls_mcp.config import WorkspaceConfig
from odoo_ls_mcp.lsp_client import LspClient
from odoo_ls_mcp.session import OdooLsSessionManager

# ── Fixtures ──────────────────────────────────────────────────────────────────


def make_config(tmp_path: Path, name: str = "ws") -> WorkspaceConfig:
    workspace = tmp_path / name
    workspace.mkdir(exist_ok=True)
    return WorkspaceConfig(
        workspace_root=workspace,
        config_path=workspace / "odools.toml",
        odools_binary=Path("/fake/odoo_ls_server"),
        idle_ttl_s=9999.0,
    )


def make_alive_client() -> LspClient:
    client = MagicMock(spec=LspClient)
    client.start = AsyncMock()
    client.stop = AsyncMock()
    type(client).is_alive = PropertyMock(return_value=True)
    return client


def make_dead_client() -> LspClient:
    client = MagicMock(spec=LspClient)
    client.start = AsyncMock()
    client.stop = AsyncMock()
    type(client).is_alive = PropertyMock(return_value=False)
    return client


# ── Tests: get_session ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_get_session_creates_client(tmp_path: Path):
    cfg = make_config(tmp_path)
    manager = OdooLsSessionManager()
    created = make_alive_client()

    with patch("odoo_ls_mcp.session.LspClient", return_value=created):
        client = await manager.get_session(cfg)

    assert client is created
    created.start.assert_awaited_once_with(cfg)
    await manager.close_all()


@pytest.mark.asyncio
async def test_second_get_session_returns_same_object(tmp_path: Path):
    cfg = make_config(tmp_path)
    manager = OdooLsSessionManager()
    created = make_alive_client()

    with patch("odoo_ls_mcp.session.LspClient", return_value=created):
        c1 = await manager.get_session(cfg)
        c2 = await manager.get_session(cfg)

    assert c1 is c2
    created.start.assert_awaited_once()
    await manager.close_all()


@pytest.mark.asyncio
async def test_crashed_session_is_transparently_restarted(tmp_path: Path):
    cfg = make_config(tmp_path)
    manager = OdooLsSessionManager()

    dead = make_dead_client()
    fresh = make_alive_client()

    manager._sessions[(str(cfg.workspace_root), str(cfg.config_path))] = dead

    with patch("odoo_ls_mcp.session.LspClient", return_value=fresh):
        client = await manager.get_session(cfg)

    assert client is fresh
    dead.stop.assert_awaited_once()
    fresh.start.assert_awaited_once_with(cfg)
    await manager.close_all()


# ── Tests: restart_session ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_restart_session_stops_old_and_starts_new(tmp_path: Path):
    cfg = make_config(tmp_path)
    manager = OdooLsSessionManager()

    old = make_alive_client()
    new = make_alive_client()

    with patch("odoo_ls_mcp.session.LspClient", return_value=old):
        await manager.get_session(cfg)

    with patch("odoo_ls_mcp.session.LspClient", return_value=new):
        restarted = await manager.restart_session(cfg)

    assert restarted is new
    old.stop.assert_awaited_once()
    new.start.assert_awaited_once_with(cfg)
    await manager.close_all()


@pytest.mark.asyncio
async def test_restart_session_when_none_existing(tmp_path: Path):
    cfg = make_config(tmp_path)
    manager = OdooLsSessionManager()
    fresh = make_alive_client()

    with patch("odoo_ls_mcp.session.LspClient", return_value=fresh):
        client = await manager.restart_session(cfg)

    assert client is fresh
    fresh.start.assert_awaited_once_with(cfg)
    await manager.close_all()


# ── Tests: close_all ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_all_stops_all_sessions(tmp_path: Path):
    manager = OdooLsSessionManager()
    cfg_a = make_config(tmp_path, "ws_a")
    cfg_b = make_config(tmp_path, "ws_b")

    clients = [make_alive_client(), make_alive_client()]
    it = iter(clients)

    with patch("odoo_ls_mcp.session.LspClient", side_effect=it):
        await manager.get_session(cfg_a)
        await manager.get_session(cfg_b)

    await manager.close_all()

    for c in clients:
        c.stop.assert_awaited_once()

    assert manager._sessions == {}


@pytest.mark.asyncio
async def test_close_all_is_idempotent(tmp_path: Path):
    cfg = make_config(tmp_path)
    manager = OdooLsSessionManager()
    client = make_alive_client()

    with patch("odoo_ls_mcp.session.LspClient", return_value=client):
        await manager.get_session(cfg)

    await manager.close_all()
    await manager.close_all()

    client.stop.assert_awaited_once()


# ── Tests: concurrent first calls ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_first_calls_create_only_one_session(tmp_path: Path):
    cfg = make_config(tmp_path)
    manager = OdooLsSessionManager()

    call_count = 0
    first_client = make_alive_client()

    async def slow_start(c: WorkspaceConfig) -> None:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)

    first_client.start = AsyncMock(side_effect=slow_start)

    with patch("odoo_ls_mcp.session.LspClient", return_value=first_client):
        results = await asyncio.gather(
            manager.get_session(cfg),
            manager.get_session(cfg),
            manager.get_session(cfg),
        )

    assert call_count == 1, f"Expected 1 start call, got {call_count}"
    assert all(r is first_client for r in results)
    await manager.close_all()


# ── Tests: idle TTL ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_idle_ttl_expires_and_stops_session(tmp_path: Path):
    cfg = make_config(tmp_path)
    cfg = WorkspaceConfig(
        workspace_root=cfg.workspace_root,
        config_path=cfg.config_path,
        odools_binary=cfg.odools_binary,
        idle_ttl_s=0.05,
    )
    manager = OdooLsSessionManager()
    client = make_alive_client()

    with patch("odoo_ls_mcp.session.LspClient", return_value=client):
        await manager.get_session(cfg)

    await asyncio.sleep(0.2)

    client.stop.assert_awaited_once()
    key = (str(cfg.workspace_root), str(cfg.config_path))
    assert key not in manager._sessions


@pytest.mark.asyncio
async def test_get_session_resets_idle_timer(tmp_path: Path):
    cfg = make_config(tmp_path)
    cfg = WorkspaceConfig(
        workspace_root=cfg.workspace_root,
        config_path=cfg.config_path,
        odools_binary=cfg.odools_binary,
        idle_ttl_s=0.1,
    )
    manager = OdooLsSessionManager()
    client = make_alive_client()

    with patch("odoo_ls_mcp.session.LspClient", return_value=client):
        await manager.get_session(cfg)
        await asyncio.sleep(0.07)
        await manager.get_session(cfg)
        await asyncio.sleep(0.07)

    client.stop.assert_not_called()

    await asyncio.sleep(0.15)
    client.stop.assert_awaited_once()
    await manager.close_all()
