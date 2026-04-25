"""
Tests for the LSP session manager.

Uses a fake LSP server (asyncio subprocess) to test framing, handshake,
diagnostics routing, and session lifecycle without requiring real OdooLS.
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import patch

import pytest

from odoo_ls_mcp.lsp_session import (
    LspSession,
    SessionRegistry,
    SessionState,
    encode_message,
    read_message,
)

# ── Unit: message framing ─────────────────────────────────────────────────────


def test_encode_message_roundtrip():
    payload = {"jsonrpc": "2.0", "method": "initialize", "params": {}}
    encoded = encode_message(payload)
    assert encoded.startswith(b"Content-Length:")
    assert b"\r\n\r\n" in encoded
    body_start = encoded.index(b"\r\n\r\n") + 4
    body = json.loads(encoded[body_start:])
    assert body == payload


def test_encode_message_content_length_accurate():
    payload = {"jsonrpc": "2.0", "id": 1, "result": {"hello": "wörld"}}
    encoded = encode_message(payload)
    header_end = encoded.index(b"\r\n\r\n")
    header = encoded[:header_end].decode()
    body = encoded[header_end + 4 :]
    content_length = int(header.split(":")[1].strip())
    assert content_length == len(body)


@pytest.mark.asyncio
async def test_read_message_basic():
    payload = {"jsonrpc": "2.0", "id": 1, "result": None}
    encoded = encode_message(payload)
    reader = asyncio.StreamReader()
    reader.feed_data(encoded)
    reader.feed_eof()
    msg = await read_message(reader)
    assert msg == payload


@pytest.mark.asyncio
async def test_read_message_eof_returns_none():
    reader = asyncio.StreamReader()
    reader.feed_eof()
    msg = await read_message(reader)
    assert msg is None


@pytest.mark.asyncio
async def test_read_message_multiple():
    """Two back-to-back messages should be read independently."""
    p1 = {"jsonrpc": "2.0", "id": 1, "result": "first"}
    p2 = {"jsonrpc": "2.0", "id": 2, "result": "second"}
    reader = asyncio.StreamReader()
    reader.feed_data(encode_message(p1) + encode_message(p2))
    reader.feed_eof()
    m1 = await read_message(reader)
    m2 = await read_message(reader)
    assert m1 == p1
    assert m2 == p2


# ── Fake LSP server fixture ───────────────────────────────────────────────────

FAKE_SERVER_SCRIPT = textwrap.dedent("""\
    import asyncio, json, sys, os

    def encode(payload):
        body = json.dumps(payload).encode()
        header = f"Content-Length: {len(body)}\\r\\n\\r\\n".encode()
        return header + body

    async def read_msg(reader):
        headers = {}
        while True:
            line = await reader.readuntil(b"\\r\\n")
            line = line.rstrip()
            if not line:
                break
            if b":" in line:
                k, _, v = line.decode().partition(":")
                headers[k.strip().lower()] = v.strip()
        length = int(headers["content-length"])
        body = await reader.readexactly(length)
        return json.loads(body)

    async def main():
        reader = asyncio.StreamReader()
        proto = asyncio.StreamReaderProtocol(reader)
        loop = asyncio.get_event_loop()
        await loop.connect_read_pipe(lambda: proto, sys.stdin.buffer)
        transport, _ = await loop.connect_write_pipe(asyncio.BaseProtocol, sys.stdout.buffer)
        writer = asyncio.StreamWriter(transport, proto, reader, loop)

        # Server-side progress token creation (simulate indexing)
        def send(payload):
            data = encode(payload)
            writer.write(data)

        sent_progress = False

        while True:
            msg = await read_msg(reader)
            method = msg.get("method", "")
            msg_id = msg.get("id")

            if method == "initialize":
                send({"jsonrpc": "2.0", "id": msg_id, "result": {
                    "capabilities": {
                        "hoverProvider": True,
                        "definitionProvider": True,
                        "completionProvider": {},
                    }
                }})
            elif method == "initialized":
                # Send a workDoneProgress/create request then progress notifications
                send({"jsonrpc": "2.0", "id": 99, "method": "window/workDoneProgress/create",
                      "params": {"token": "indexing-1"}})
                await asyncio.sleep(0.05)
                send({"jsonrpc": "2.0", "method": "$/progress",
                      "params": {"token": "indexing-1",
                                 "value": {"kind": "begin", "title": "Indexing"}}})
                await asyncio.sleep(0.05)
                send({"jsonrpc": "2.0", "method": "$/progress",
                      "params": {"token": "indexing-1",
                                 "value": {"kind": "end", "message": "Done"}}})
                # Push sample diagnostics
                send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                      "params": {
                          "uri": "file:///ws/addon/__manifest__.py",
                          "diagnostics": [{
                              "range": {"start": {"line": 0, "character": 0},
                                        "end": {"line": 0, "character": 5}},
                              "severity": 1,
                              "code": "OLS04001",
                              "source": "OdooLS",
                              "message": "Missing required key",
                          }]
                      }})
            elif method == "textDocument/hover":
                send({"jsonrpc": "2.0", "id": msg_id, "result": {
                    "contents": {"kind": "markdown", "value": "**res.partner** field"}
                }})
            elif method == "textDocument/definition":
                send({"jsonrpc": "2.0", "id": msg_id, "result": [{
                    "uri": "file:///odoo/addons/base/models/res_partner.py",
                    "range": {"start": {"line": 42, "character": 4},
                              "end": {"line": 42, "character": 20}},
                }]})
            elif method == "textDocument/completion":
                send({"jsonrpc": "2.0", "id": msg_id, "result": {
                    "items": [
                        {"label": "partner_id", "kind": 5, "detail": "Many2one"},
                        {"label": "name", "kind": 5, "detail": "Char"},
                    ]
                }})
            elif method == "textDocument/didOpen":
                pass  # no response needed
            elif method == "shutdown":
                send({"jsonrpc": "2.0", "id": msg_id, "result": None})
            elif method == "exit":
                break
            elif msg_id is not None:
                # respond to unknown requests with null
                send({"jsonrpc": "2.0", "id": msg_id, "result": None})
            # ignore unknown notifications

    asyncio.run(main())
""")


@pytest.fixture
async def fake_server_path(tmp_path: Path) -> Path:
    script = tmp_path / "fake_odoo_ls.py"
    script.write_text(FAKE_SERVER_SCRIPT, encoding="utf-8")
    return script


@pytest.fixture
async def session(
    tmp_path: Path, fake_server_path: Path
) -> AsyncGenerator[LspSession, None]:
    """An LspSession backed by the fake LSP server."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "odools.toml").write_text("[[config]]\nodoo_path='/odoo'\n")

    sess = LspSession(workspace=workspace, binary=sys.executable)

    # Patch _build_command to use our fake server
    def patched_build():
        return [sys.executable, str(fake_server_path)]

    sess._build_command = patched_build

    await sess.start()
    yield sess
    await sess.stop()


# ── Session tests ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_starts_and_becomes_ready(session: LspSession):
    assert session.is_ready


@pytest.mark.asyncio
async def test_session_indexing_completes(session: LspSession):
    indexed = await session.wait_for_indexing(timeout=5.0)
    assert indexed
    assert session.state == SessionState.READY


@pytest.mark.asyncio
async def test_session_receives_diagnostics(session: LspSession):
    # Indexing triggers diagnostics push in fake server
    await session.wait_for_indexing(timeout=5.0)
    # Give a moment for the publishDiagnostics notification to arrive
    await asyncio.sleep(0.2)
    all_diags = session.get_all_diagnostics()
    assert len(all_diags) > 0
    flat = [d for diags in all_diags.values() for d in diags]
    assert any(d.code == "OLS04001" for d in flat)


@pytest.mark.asyncio
async def test_hover_returns_content(session: LspSession, tmp_path: Path):
    await session.wait_for_indexing(timeout=5.0)
    test_file = tmp_path / "workspace" / "test.py"
    test_file.write_text("partner_id = fields.Many2one('res.partner')\n")
    result = await session.hover(test_file, line=0, character=0)
    assert result is not None
    contents = result.get("contents", {})
    assert "res.partner" in str(contents)


@pytest.mark.asyncio
async def test_go_to_definition_returns_locations(session: LspSession, tmp_path: Path):
    await session.wait_for_indexing(timeout=5.0)
    test_file = tmp_path / "workspace" / "test.py"
    test_file.write_text("partner_id = fields.Many2one('res.partner')\n")
    locations = await session.go_to_definition(test_file, line=0, character=0)
    assert len(locations) > 0
    assert "res_partner" in locations[0]["uri"]


@pytest.mark.asyncio
async def test_completions_returns_items(session: LspSession, tmp_path: Path):
    await session.wait_for_indexing(timeout=5.0)
    test_file = tmp_path / "workspace" / "test.py"
    test_file.write_text("self.\n")
    items = await session.completions(test_file, line=0, character=5)
    assert len(items) == 2
    labels = [i["label"] for i in items]
    assert "partner_id" in labels
    assert "name" in labels


@pytest.mark.asyncio
async def test_session_stop_cleans_up(session: LspSession):
    await session.stop()
    assert session.state == SessionState.STOPPED


@pytest.mark.asyncio
async def test_session_state_after_failure(tmp_path: Path):
    """A session pointed at a non-existent binary should fail cleanly."""
    workspace = tmp_path / "ws2"
    workspace.mkdir()
    sess = LspSession(workspace=workspace, binary="/nonexistent/binary/odoo_ls_server")
    with pytest.raises((FileNotFoundError, OSError)):
        await sess.start()
    assert sess.state == SessionState.FAILED


# ── Registry tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_registry_returns_same_session(tmp_path: Path, fake_server_path: Path):
    workspace = tmp_path / "ws_reg"
    workspace.mkdir()

    registry = SessionRegistry()

    async def patched_get_or_create(ws: Path, cfg: Path | None = None) -> LspSession:
        sess = LspSession(workspace=ws, config_path=cfg, binary=sys.executable)
        sess._build_command = lambda: [sys.executable, str(fake_server_path)]
        registry._sessions[registry._session_key(ws, cfg)] = sess
        registry._config_mtimes[registry._session_key(ws, cfg)] = None
        await sess.start()
        return sess

    with patch.object(registry, "get_or_create", patched_get_or_create):
        s1 = await registry.get_or_create(workspace)
        # Second call returns cached session
        s2 = await registry.get(workspace)

    assert s1 is s2
    await registry.stop_all()


@pytest.mark.asyncio
async def test_registry_status(tmp_path: Path, fake_server_path: Path):
    workspace = tmp_path / "ws_status"
    workspace.mkdir()

    registry = SessionRegistry()
    sess = LspSession(workspace=workspace, config_path=None, binary=sys.executable)
    sess._build_command = lambda: [sys.executable, str(fake_server_path)]
    await sess.start()
    registry._sessions[registry._session_key(workspace, None)] = sess

    status = registry.status()
    key = (str(workspace.resolve()), None)
    assert key in status
    assert status[key] in ("INDEXING", "READY")

    await registry.stop_all()


@pytest.mark.asyncio
async def test_registry_distinguishes_config_paths(
    tmp_path: Path, fake_server_path: Path
):
    workspace = tmp_path / "ws_config_key"
    workspace.mkdir()
    config_a = workspace / "a.toml"
    config_b = workspace / "b.toml"
    config_a.write_text("")
    config_b.write_text("")

    registry = SessionRegistry()
    original_start = LspSession.start

    async def patched_start(self: LspSession) -> None:
        if self.binary is None:
            self.binary = sys.executable
        self._build_command = lambda: [sys.executable, str(fake_server_path)]
        await original_start(self)

    with patch.object(LspSession, "start", patched_start):
        sess_a = await registry.get_or_create(workspace, config_a)
        sess_b = await registry.get_or_create(workspace, config_b)

    assert sess_a is not sess_b
    assert await registry.get(workspace, config_a) is sess_a
    assert await registry.get(workspace, config_b) is sess_b
    await registry.stop_all()


@pytest.mark.asyncio
async def test_registry_evicts_idle_sessions(
    tmp_path: Path, fake_server_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "ws_ttl"
    workspace.mkdir()
    monkeypatch.setenv("ODOO_LS_IDLE_TTL", "0.2")

    registry = SessionRegistry()
    original_start = LspSession.start

    async def patched_start(self: LspSession) -> None:
        if self.binary is None:
            self.binary = sys.executable
        self._build_command = lambda: [sys.executable, str(fake_server_path)]
        await original_start(self)

    with patch.object(LspSession, "start", patched_start):
        session = await registry.get_or_create(workspace)
        await session.wait_for_indexing(timeout=5.0)
        await asyncio.sleep(0.35)
        await asyncio.sleep(0.2)

        assert await registry.get(workspace) is None
        assert session.state == SessionState.STOPPED

    await registry.stop_all()


@pytest.mark.asyncio
async def test_registry_refreshes_last_used_on_get_or_create(
    tmp_path: Path, fake_server_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "ws_ttl_touch"
    workspace.mkdir()
    monkeypatch.setenv("ODOO_LS_IDLE_TTL", "60")

    registry = SessionRegistry()
    original_start = LspSession.start

    async def patched_start(self: LspSession) -> None:
        if self.binary is None:
            self.binary = sys.executable
        self._build_command = lambda: [sys.executable, str(fake_server_path)]
        await original_start(self)

    with patch.object(LspSession, "start", patched_start):
        session = await registry.get_or_create(workspace)
        await session.wait_for_indexing(timeout=5.0)
        key = registry._session_key(workspace)
        first_last_used = registry._last_used[key]
        await asyncio.sleep(0.15)

        same_session = await registry.get_or_create(workspace)
        assert same_session is session
        assert registry._last_used[key] > first_last_used

        assert await registry.get(workspace) is session

    await registry.stop_all()
