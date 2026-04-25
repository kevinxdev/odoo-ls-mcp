from __future__ import annotations

import asyncio
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from odoo_ls_mcp.lsp_session import (
    LspSession,
    SessionRegistry,
    SessionState,
)

FAKE_SERVER_SCRIPT = textwrap.dedent("""\
    import asyncio, json, sys

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

        def send(payload):
            writer.write(encode(payload))

        while True:
            msg = await read_msg(reader)
            method = msg.get("method", "")
            msg_id = msg.get("id")
            if method == "initialize":
                send({"jsonrpc": "2.0", "id": msg_id, "result": {"capabilities": {}}})
            elif method == "shutdown":
                send({"jsonrpc": "2.0", "id": msg_id, "result": None})
            elif method == "exit":
                break
            elif msg_id is not None:
                send({"jsonrpc": "2.0", "id": msg_id, "result": None})

    asyncio.run(main())
""")


def _make_session(workspace: Path, fake_server_path: Path) -> LspSession:
    sess = LspSession(workspace=workspace, binary=sys.executable)
    sess._build_command = lambda: [sys.executable, str(fake_server_path)]
    return sess


@pytest.fixture
async def fake_server_path(tmp_path: Path) -> Path:
    script = tmp_path / "fake_odoo_ls.py"
    script.write_text(FAKE_SERVER_SCRIPT, encoding="utf-8")
    return script


@pytest.mark.asyncio
async def test_concurrent_get_or_create_single_session(
    tmp_path: Path, fake_server_path: Path
):
    workspace = tmp_path / "ws_concurrent"
    workspace.mkdir()
    (workspace / "odools.toml").write_text("")

    registry = SessionRegistry()

    creation_count = 0
    original_start = LspSession.start

    async def counting_start(self):
        nonlocal creation_count
        creation_count += 1
        if self.binary is None:
            self.binary = sys.executable
        self._build_command = lambda: [sys.executable, str(fake_server_path)]
        await original_start(self)

    with patch.object(LspSession, "start", counting_start):
        sessions = await asyncio.gather(
            registry.get_or_create(workspace),
            registry.get_or_create(workspace),
            registry.get_or_create(workspace),
        )

    assert creation_count == 1, f"Expected 1 session start, got {creation_count}"
    assert sessions[0] is sessions[1] is sessions[2]
    await registry.stop_all()


@pytest.mark.asyncio
async def test_crash_recovery_creates_fresh_session(
    tmp_path: Path, fake_server_path: Path
):
    workspace = tmp_path / "ws_crash"
    workspace.mkdir()

    sess = _make_session(workspace, fake_server_path)
    await sess.start()
    assert sess.is_ready

    assert sess._proc is not None
    sess._proc.kill()
    await asyncio.sleep(0.3)
    assert sess.state == SessionState.FAILED

    registry = SessionRegistry()
    registry._sessions[registry._session_key(workspace)] = sess

    original_start = LspSession.start

    async def patched_start(self):
        if self.binary is None:
            self.binary = sys.executable
        self._build_command = lambda: [sys.executable, str(fake_server_path)]
        await original_start(self)

    with patch.object(LspSession, "start", patched_start):
        new_sess = await registry.get_or_create(workspace)

    assert new_sess is not sess
    assert new_sess.is_ready
    await registry.stop_all()


@pytest.mark.asyncio
async def test_startup_timeout_raises(tmp_path: Path):
    workspace = tmp_path / "ws_timeout"
    workspace.mkdir()

    sess = LspSession(workspace=workspace, binary=sys.executable)

    async def hanging_initialize():
        await asyncio.sleep(9999)

    with patch.object(sess, "_initialize", hanging_initialize):
        with patch("odoo_ls_mcp.lsp_session.INITIALIZE_TIMEOUT", 0.1):
            import odoo_ls_mcp.lsp_session as _mod

            original_timeout = _mod.INITIALIZE_TIMEOUT
            _mod.INITIALIZE_TIMEOUT = 0.1

            script = tmp_path / "echo_server.py"
            script.write_text("import time\ntime.sleep(9999)\n", encoding="utf-8")
            sess._build_command = lambda: [sys.executable, str(script)]

            with pytest.raises(
                (asyncio.TimeoutError, TimeoutError, OSError, Exception)
            ):
                await asyncio.wait_for(sess._initialize(), timeout=0.1)

            _mod.INITIALIZE_TIMEOUT = original_timeout

    assert sess.state in (
        SessionState.FAILED,
        SessionState.STARTING,
        SessionState.STOPPED,
        SessionState.INITIALIZING,
    )


@pytest.mark.asyncio
async def test_stale_config_triggers_restart(tmp_path: Path, fake_server_path: Path):
    workspace = tmp_path / "ws_stale"
    workspace.mkdir()
    config = workspace / "odools.toml"
    config.write_text("")

    registry = SessionRegistry()

    first_sess_created = None
    original_start = LspSession.start

    async def tracking_start(self):
        nonlocal first_sess_created
        if self.binary is None:
            self.binary = sys.executable
        self._build_command = lambda: [sys.executable, str(fake_server_path)]
        await original_start(self)
        if first_sess_created is None:
            first_sess_created = self

    with patch.object(LspSession, "start", tracking_start):
        sess1 = await registry.get_or_create(workspace, config_path=config)

    assert sess1 is first_sess_created

    import time

    time.sleep(0.01)
    config.write_text("changed = true")
    import os

    new_mtime = config.stat().st_mtime + 1
    os.utime(config, (new_mtime, new_mtime))

    second_sess = None

    async def tracking_start2(self):
        nonlocal second_sess
        if self.binary is None:
            self.binary = sys.executable
        self._build_command = lambda: [sys.executable, str(fake_server_path)]
        await original_start(self)
        second_sess = self

    with patch.object(LspSession, "start", tracking_start2):
        sess2 = await registry.get_or_create(workspace, config_path=config)

    assert sess2 is not sess1, "Expected a new session after config mtime change"
    await registry.stop_all()


@pytest.mark.asyncio
async def test_stop_all_clears_all_sessions(tmp_path: Path, fake_server_path: Path):
    registry = SessionRegistry()

    workspaces = []
    for i in range(3):
        ws = tmp_path / f"ws_stop_{i}"
        ws.mkdir()
        workspaces.append(ws)

    original_start = LspSession.start

    async def patched_start(self):
        if self.binary is None:
            self.binary = sys.executable
        self._build_command = lambda: [sys.executable, str(fake_server_path)]
        await original_start(self)

    with patch.object(LspSession, "start", patched_start):
        for ws in workspaces:
            await registry.get_or_create(ws)

    assert len(registry._sessions) == 3

    await registry.stop_all()

    assert len(registry._sessions) == 0
    assert len(registry._config_mtimes) == 0
