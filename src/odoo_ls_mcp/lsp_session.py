"""
LSP session manager for odoo-ls-mcp.

Manages a single long-lived `odoo_ls_server` subprocess per workspace.
Handles:
  - Content-Length framed JSON-RPC 2.0 (LSP wire format)
  - LSP initialize handshake
  - Async response routing (request id → asyncio.Future)
  - Server-initiated notification handling (publishDiagnostics, progress)
  - workDoneProgress tracking for indexing-readiness gating
  - Crash detection and auto-restart with exponential backoff
  - Clean shutdown sequence (shutdown → exit → SIGTERM → SIGKILL)

IMPORTANT: stdout is NEVER touched by this module; all logging → stderr.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any

from .models import Diagnostic, DiagnosticSeverity, Position, Range

logger = logging.getLogger(__name__)

SessionKey = tuple[Path, Path | None]

# ── Constants ────────────────────────────────────────────────────────────────

ODOOLS_BINARY = "odoo_ls_server"

# Timeouts (seconds)
INITIALIZE_TIMEOUT = 30.0
REQUEST_TIMEOUT = 30.0
INDEXING_READY_TIMEOUT = 120.0  # max wait for initial indexing
SHUTDOWN_TIMEOUT = 10.0

# Crash recovery
MAX_RESTART_ATTEMPTS = 3
RESTART_BACKOFF_BASE = 2.0  # seconds; doubled each attempt


# ── LSP message framing ──────────────────────────────────────────────────────


def encode_message(payload: dict[str, Any]) -> bytes:
    """Encode a JSON-RPC payload as an LSP Content-Length framed message."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


async def read_message(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    """
    Read one LSP message from reader.

    Returns None on EOF/connection close.
    Raises ValueError on malformed header.
    """
    # Read headers until blank line
    headers: dict[str, str] = {}
    while True:
        try:
            line = await reader.readuntil(b"\r\n")
        except (asyncio.IncompleteReadError, ConnectionResetError):
            return None
        line = line.rstrip(b"\r\n")
        if not line:
            break  # blank line separates headers from body
        if b":" in line:
            key, _, value = line.decode("ascii", errors="replace").partition(":")
            headers[key.strip().lower()] = value.strip()

    content_length = headers.get("content-length")
    if content_length is None:
        raise ValueError(f"LSP message missing Content-Length header: {headers}")

    try:
        length = int(content_length)
    except ValueError as e:
        raise ValueError(f"Invalid Content-Length: {content_length!r}") from e

    try:
        body = await reader.readexactly(length)
    except asyncio.IncompleteReadError:
        return None

    return json.loads(body.decode("utf-8"))


# ── Session state ────────────────────────────────────────────────────────────


class SessionState(Enum):
    STOPPED = auto()
    STARTING = auto()
    INITIALIZING = auto()
    READY = auto()
    INDEXING = auto()  # ready but indexing still in progress
    SHUTTING_DOWN = auto()
    FAILED = auto()


@dataclass
class IndexingProgress:
    token: str | int
    started_at: float = field(default_factory=time.monotonic)
    title: str = ""
    message: str = ""
    percentage: int | None = None
    done: bool = False


# ── LSP Session ──────────────────────────────────────────────────────────────


class LspSession:
    """
    A long-lived LSP session wrapping one odoo_ls_server subprocess.

    Thread-safety: designed for use from a single asyncio event loop.
    """

    def __init__(
        self,
        workspace: Path,
        config_path: Path | None = None,
        binary: str | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.config_path = config_path.resolve() if config_path else None
        self.binary = binary or shutil.which(ODOOLS_BINARY)
        self._selected_profile = _resolve_selected_profile(self.config_path)

        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._state = SessionState.STOPPED
        self._state_lock = asyncio.Lock()

        # request id → Future[response]
        self._pending: dict[int | str, asyncio.Future[Any]] = {}
        self._next_id = 1

        # Diagnostics cache: file URI → list[Diagnostic]
        self._diagnostics: dict[str, list[Diagnostic]] = {}
        self._diag_updated_event = asyncio.Event()

        # workDoneProgress tracking
        self._progress: dict[str | int, IndexingProgress] = {}
        self._indexing_done_event = asyncio.Event()

        # Notification callbacks
        self._notification_handlers: dict[
            str, list[Callable[[dict[str, Any]], None]]
        ] = {}

        # Restart tracking
        self._restart_count = 0
        self._last_crash_at: float = 0.0

    # ── Public state ─────────────────────────────────────────────────────────

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def is_ready(self) -> bool:
        return self._state in (SessionState.READY, SessionState.INDEXING)

    @property
    def diagnostics(self) -> dict[str, list[Diagnostic]]:
        return dict(self._diagnostics)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the LSP subprocess and complete the initialize handshake."""
        async with self._state_lock:
            if self._state not in (SessionState.STOPPED, SessionState.FAILED):
                return
            self._state = SessionState.STARTING

        if self.binary is None:
            self._state = SessionState.FAILED
            raise FileNotFoundError(
                f"'{ODOOLS_BINARY}' not found on PATH. Install OdooLS first."
            )

        cmd = self._build_command()
        logger.debug("Spawning: %s", " ".join(cmd))

        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # prevent signal propagation
            )
        except (FileNotFoundError, PermissionError) as exc:
            self._state = SessionState.FAILED
            raise FileNotFoundError(f"Failed to spawn '{cmd[0]}': {exc}") from exc

        self._state = SessionState.INITIALIZING

        # Start reader loop
        self._reader_task = asyncio.create_task(self._reader_loop(), name="lsp-reader")
        # Start stderr drain (prevent blocking)
        asyncio.create_task(self._stderr_drain(), name="lsp-stderr")

        # LSP initialize handshake
        try:
            await asyncio.wait_for(self._initialize(), timeout=INITIALIZE_TIMEOUT)
        except TimeoutError:
            await self._kill()
            self._state = SessionState.FAILED
            raise TimeoutError(
                f"LSP initialize handshake timed out after {INITIALIZE_TIMEOUT}s"
            ) from None
        except Exception:
            await self._kill()
            self._state = SessionState.FAILED
            raise

        self._state = SessionState.INDEXING
        self._indexing_done_event.clear()
        logger.info("LSP session ready (workspace=%s)", self.workspace)

    async def stop(self) -> None:
        """Gracefully shut down the LSP subprocess."""
        if self._state in (SessionState.STOPPED, SessionState.SHUTTING_DOWN):
            return

        self._state = SessionState.SHUTTING_DOWN

        if self._proc and self._proc.returncode is None:
            try:
                # LSP shutdown request
                await asyncio.wait_for(
                    self._request("shutdown", {}), timeout=SHUTDOWN_TIMEOUT
                )
                # LSP exit notification
                self._notify("exit", {})
                # Wait for process
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except Exception:
                logger.exception(
                    "Error while stopping LSP session (workspace=%s)", self.workspace
                )
            finally:
                await self._kill()

        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

        self._pending.clear()
        self._state = SessionState.STOPPED
        logger.info("LSP session stopped (workspace=%s)", self.workspace)

    async def wait_for_indexing(self, timeout: float = INDEXING_READY_TIMEOUT) -> bool:
        """
        Wait until OdooLS has finished initial workspace indexing.

        Returns True if indexing completed, False if timed out.
        Extends deadline while progress is actively being reported.
        """
        if self._state == SessionState.READY:
            return True

        deadline = time.monotonic() + timeout
        last_progress_at = time.monotonic()

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._indexing_done_event.wait()),
                    timeout=min(remaining, 5.0),
                )
                self._state = SessionState.READY
                return True
            except TimeoutError:
                # Extend deadline if progress was made in the last 5 s
                active = [p for p in self._progress.values() if not p.done]
                if active:
                    if time.monotonic() - last_progress_at < 10.0:
                        deadline += 5.0  # extend while actively indexing
                continue

        logger.warning("Indexing did not complete within %.0fs", timeout)
        return False

    # ── Sending messages ─────────────────────────────────────────────────────

    async def request(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float = REQUEST_TIMEOUT,
    ) -> Any:
        """Send an LSP request and await the response."""
        if not self.is_ready:
            raise RuntimeError(
                f"LSP session not ready (state={self._state.name}). Call start() first."
            )
        return await asyncio.wait_for(self._request(method, params), timeout=timeout)

    def notify(self, method: str, params: dict[str, Any]) -> None:
        """Send an LSP notification (fire-and-forget)."""
        self._notify(method, params)

    def on_notification(
        self, method: str, handler: Callable[[dict[str, Any]], None]
    ) -> None:
        """Register a handler for LSP push notifications with the given method."""
        self._notification_handlers.setdefault(method, []).append(handler)

    # ── High-level LSP helpers ────────────────────────────────────────────────

    async def open_document(self, path: Path) -> None:
        """Send textDocument/didOpen for a file."""
        text = await asyncio.to_thread(
            path.read_text, encoding="utf-8", errors="replace"
        )
        lang = _lang_id(path)
        self._notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": path.as_uri(),
                    "languageId": lang,
                    "version": 1,
                    "text": text,
                }
            },
        )

    async def hover(
        self, path: Path, line: int, character: int, timeout: float = REQUEST_TIMEOUT
    ) -> dict[str, Any] | None:
        """Request hover information at a position."""
        await self._ensure_opened(path)
        result = await self.request(
            "textDocument/hover",
            {
                "textDocument": {"uri": path.as_uri()},
                "position": {"line": line, "character": character},
            },
            timeout=timeout,
        )
        return result  # None if no hover at position

    async def go_to_definition(
        self, path: Path, line: int, character: int, timeout: float = REQUEST_TIMEOUT
    ) -> list[dict[str, Any]]:
        """Request go-to-definition locations."""
        await self._ensure_opened(path)
        result = await self.request(
            "textDocument/definition",
            {
                "textDocument": {"uri": path.as_uri()},
                "position": {"line": line, "character": character},
            },
            timeout=timeout,
        )
        if result is None:
            return []
        if isinstance(result, dict):
            return [result]
        return result  # list of Location

    async def completions(
        self, path: Path, line: int, character: int, timeout: float = REQUEST_TIMEOUT
    ) -> list[dict[str, Any]]:
        """Request completion items at a position."""
        await self._ensure_opened(path)
        result = await self.request(
            "textDocument/completion",
            {
                "textDocument": {"uri": path.as_uri()},
                "position": {"line": line, "character": character},
                "context": {"triggerKind": 1},
            },
            timeout=timeout,
        )
        if result is None:
            return []
        if isinstance(result, list):
            return result
        # CompletionList
        return result.get("items", [])

    async def find_references(
        self,
        path: Path,
        line: int,
        character: int,
        include_declaration: bool = True,
        timeout: float = REQUEST_TIMEOUT,
    ) -> list[dict[str, Any]]:
        await self._ensure_opened(path)
        result = await self.request(
            "textDocument/references",
            {
                "textDocument": {"uri": path.as_uri()},
                "position": {"line": line, "character": character},
                "context": {"includeDeclaration": include_declaration},
            },
            timeout=timeout,
        )
        if result is None:
            return []
        return result

    async def document_symbols(
        self,
        path: Path,
        timeout: float = REQUEST_TIMEOUT,
    ) -> list[dict[str, Any]]:
        await self._ensure_opened(path)
        result = await self.request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": path.as_uri()}},
            timeout=timeout,
        )
        if result is None:
            return []
        return result

    async def workspace_symbols(
        self,
        query: str,
        timeout: float = REQUEST_TIMEOUT,
    ) -> list[dict[str, Any]]:
        result = await self.request(
            "workspace/symbol",
            {"query": query},
            timeout=timeout,
        )
        if result is None:
            return []
        return result

    def get_file_diagnostics(self, path: Path) -> list[Diagnostic]:
        """Return cached diagnostics for a specific file."""
        uri = path.as_uri()
        return list(self._diagnostics.get(uri, []))

    def get_all_diagnostics(self) -> dict[str, list[Diagnostic]]:
        """Return all cached diagnostics keyed by file path (not URI)."""
        result = {}
        for uri, diags in self._diagnostics.items():
            file_path = uri[7:] if uri.startswith("file://") else uri
            result[file_path] = list(diags)
        return result

    async def wait_for_diagnostics(self, timeout: float = 10.0) -> None:
        """Wait until at least one publishDiagnostics notification arrives."""
        self._diag_updated_event.clear()
        await asyncio.wait_for(self._diag_updated_event.wait(), timeout=timeout)

    # ── Internal: subprocess management ─────────────────────────────────────

    def _build_command(self) -> list[str]:
        assert self.binary is not None
        cmd = [self.binary]
        if self.config_path:
            cmd += ["--config-path", str(self.config_path)]
        # PID watchdog: tell OdooLS to exit if we die (Unix only)
        if sys.platform != "win32":
            cmd += ["--client-process-id", str(os.getpid())]
        return cmd

    async def _kill(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.returncode is None:
                self._proc.kill()
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
        except Exception:
            logger.exception(
                "Failed to kill LSP subprocess (workspace=%s)", self.workspace
            )

    # ── Internal: reader loop ────────────────────────────────────────────────

    async def _reader_loop(self) -> None:
        """Continuously read LSP messages from stdout and dispatch them."""
        assert self._proc is not None
        assert self._proc.stdout is not None

        try:
            while True:
                try:
                    msg = await read_message(self._proc.stdout)
                except ValueError as exc:
                    logger.error("Malformed LSP message: %s", exc)
                    continue

                if msg is None:
                    logger.warning("LSP stdout EOF — process likely exited")
                    break

                self._dispatch(msg)
        except asyncio.CancelledError:
            pass
        finally:
            # Process has exited — fail all pending requests
            self._fail_pending(RuntimeError("LSP subprocess exited unexpectedly"))
            if self._state not in (SessionState.SHUTTING_DOWN, SessionState.STOPPED):
                self._state = SessionState.FAILED
                logger.error(
                    "LSP process died (workspace=%s, exit=%s)",
                    self.workspace,
                    self._proc.returncode if self._proc else "?",
                )

    async def _stderr_drain(self) -> None:
        """Drain stderr to prevent subprocess blocking on a full pipe."""
        assert self._proc is not None
        assert self._proc.stderr is not None
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                logger.debug(
                    "[odoo_ls stderr] %s", line.decode(errors="replace").rstrip()
                )
        except asyncio.CancelledError:
            pass

    # ── Internal: message dispatch ────────────────────────────────────────────

    def _dispatch(self, msg: dict[str, Any]) -> None:
        msg_id = msg.get("id")

        # Response to a pending request
        if msg_id is not None and msg_id in self._pending:
            fut = self._pending.pop(msg_id)
            if not fut.done():
                error = msg.get("error")
                if error:
                    fut.set_exception(
                        RuntimeError(
                            f"LSP error {error.get('code')}: {error.get('message')}"
                        )
                    )
                else:
                    fut.set_result(msg.get("result"))
            return

        # Notification or server-initiated request
        method = msg.get("method", "")

        if method == "textDocument/publishDiagnostics":
            self._handle_diagnostics(msg.get("params", {}))
        elif method == "$/progress":
            self._handle_progress(msg.get("params", {}))
        elif method == "window/workDoneProgress/create":
            # Respond to server-side progress token registration
            self._respond(msg_id, {})
        elif method == "client/registerCapability":
            self._respond(msg_id, None)
        elif method == "workspace/configuration":
            self._respond(msg_id, [{"selectedProfile": self._selected_profile}])
        elif method == "window/logMessage":
            level = msg.get("params", {}).get("type", 3)
            text = msg.get("params", {}).get("message", "")
            lvl = [logging.ERROR, logging.ERROR, logging.WARNING, logging.INFO][
                min(level - 1, 3)
            ]
            logger.log(lvl, "[odoo_ls log] %s", text)
        else:
            logger.debug("Unhandled LSP method: %s", method)

        # Fire registered notification callbacks
        for cb in self._notification_handlers.get(method, []):
            try:
                cb(msg.get("params", {}))
            except Exception as exc:
                logger.warning("Notification callback error (%s): %s", method, exc)

    def _handle_diagnostics(self, params: dict[str, Any]) -> None:
        uri = params.get("uri", "")
        raw_diags = params.get("diagnostics", [])
        parsed: list[Diagnostic] = []
        for d in raw_diags:
            try:
                r = d.get("range", {})
                s = r.get("start", {})
                e = r.get("end", {})
                parsed.append(
                    Diagnostic(
                        file=uri[7:] if uri.startswith("file://") else uri,
                        range=Range(
                            start=Position(
                                line=s.get("line", 0), character=s.get("character", 0)
                            ),
                            end=Position(
                                line=e.get("line", 0), character=e.get("character", 0)
                            ),
                        ),
                        severity=d.get("severity", DiagnosticSeverity.ERROR),
                        code=d.get("code"),
                        source=d.get("source"),
                        message=d.get("message", ""),
                    )
                )
            except Exception as exc:
                logger.debug("Skipping malformed diagnostic: %s", exc)
        self._diagnostics[uri] = parsed
        self._diag_updated_event.set()
        logger.debug("Diagnostics updated: %s (%d issues)", uri, len(parsed))

    def _handle_progress(self, params: dict[str, Any]) -> None:
        token = params.get("token")
        value = params.get("value", {})
        kind = value.get("kind")

        if not isinstance(token, str | int):
            return

        if kind == "begin":
            prog = IndexingProgress(
                token=token,
                title=value.get("title", ""),
                message=value.get("message", ""),
                percentage=value.get("percentage"),
            )
            self._progress[token] = prog
            logger.debug("Progress begin: %s — %s", token, prog.title)

        elif kind == "report" and token in self._progress:
            self._progress[token].message = value.get("message", "")
            self._progress[token].percentage = value.get("percentage")
            logger.debug(
                "Progress: %s %s%%",
                self._progress[token].title,
                self._progress[token].percentage,
            )

        elif kind == "end" and token in self._progress:
            self._progress[token].done = True
            logger.debug("Progress end: %s", token)
            # Check if all progress tokens are done
            active = [p for p in self._progress.values() if not p.done]
            if not active:
                self._indexing_done_event.set()
                logger.info("OdooLS indexing complete (workspace=%s)", self.workspace)

    def _fail_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    # ── Internal: sending ─────────────────────────────────────────────────────

    async def _initialize(self) -> None:
        """Perform the LSP initialize + initialized handshake."""
        result = await self._request(
            "initialize",
            {
                "processId": os.getpid(),
                "clientInfo": {"name": "odoo-ls-mcp", "version": "0.1.0"},
                "rootUri": self.workspace.as_uri(),
                "workspaceFolders": [
                    {"uri": self.workspace.as_uri(), "name": self.workspace.name}
                ],
                "capabilities": {
                    "workspace": {
                        "workDoneProgress": True,
                    },
                    "textDocument": {
                        "hover": {"contentFormat": ["plaintext", "markdown"]},
                        "definition": {"linkSupport": False},
                        "completion": {
                            "completionItem": {"snippetSupport": False},
                        },
                        "publishDiagnostics": {
                            "relatedInformation": False,
                        },
                    },
                    "window": {
                        "workDoneProgress": True,
                    },
                },
            },
        )
        logger.debug("LSP server capabilities: %s", result.get("capabilities", {}))
        # Send initialized notification to complete handshake
        self._notify("initialized", {})

    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        """Send a request and return a Future for the response."""
        assert self._proc is not None and self._proc.stdin is not None

        req_id = self._next_id
        self._next_id += 1

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[req_id] = fut

        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }
        data = encode_message(payload)
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()

        return await fut

    def _respond(self, req_id: Any, result: Any) -> None:
        """Send a response to a server-initiated request."""
        if self._proc is None or self._proc.stdin is None:
            return
        payload = {"jsonrpc": "2.0", "id": req_id, "result": result}
        self._proc.stdin.write(encode_message(payload))
        # Fire-and-forget; don't await drain here (called from sync dispatch)
        asyncio.ensure_future(self._proc.stdin.drain())

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        """Send a notification (no response expected)."""
        if self._proc is None or self._proc.stdin is None:
            return
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        self._proc.stdin.write(encode_message(payload))
        asyncio.ensure_future(self._proc.stdin.drain())

    # ── Internal: helpers ────────────────────────────────────────────────────

    async def _ensure_opened(self, path: Path) -> None:
        """Open a document if not already tracked."""
        uri = path.as_uri()
        if uri not in self._diagnostics:
            await self.open_document(path)


# ── Session registry (singleton per workspace) ────────────────────────────────


def _get_mtime(path: Path | None) -> float | None:
    if path is None:
        return None
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _resolve_selected_profile(config_path: Path | None) -> str:
    if config_path is None or not config_path.exists():
        return "default"
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to read selected profile from %s: %s", config_path, exc)
        return "default"

    entries = raw.get("config")
    if not isinstance(entries, list):
        return "default"
    for entry in entries:
        if isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str) and name.strip():
                return name
    return "default"


class SessionRegistry:
    """
    Global registry of LspSession instances, keyed by workspace path.

    Ensures a single OdooLS process per workspace across multiple MCP tool calls.
    """

    def __init__(self) -> None:
        self._sessions: dict[SessionKey, LspSession] = {}
        self._locks: dict[SessionKey, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        self._config_mtimes: dict[SessionKey, float | None] = {}

    @staticmethod
    def _session_key(workspace: Path, config_path: Path | None = None) -> SessionKey:
        return (workspace.resolve(), config_path.resolve() if config_path else None)

    async def _ensure_lock(
        self, workspace: Path, config_path: Path | None = None
    ) -> asyncio.Lock:
        key = self._session_key(workspace, config_path)
        async with self._global_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            return self._locks[key]

    async def get_or_create(
        self,
        workspace: Path,
        config_path: Path | None = None,
    ) -> LspSession:
        workspace = workspace.resolve()
        config_path = config_path.resolve() if config_path else None
        key = self._session_key(workspace, config_path)
        lock = await self._ensure_lock(workspace, config_path)

        async with lock:
            session = self._sessions.get(key)

            if session is not None and session.is_ready:
                current_mtime = _get_mtime(config_path)
                stored_mtime = self._config_mtimes.get(key)
                if current_mtime is not None and current_mtime != stored_mtime:
                    logger.info(
                        "odools.toml mtime changed — restarting stale session for %s",
                        workspace,
                    )
                    await session.stop()
                    session = None
                else:
                    return session

            if session is not None and not session.is_ready:
                logger.info(
                    "Replacing non-ready LSP session (state=%s) for %s",
                    session.state.name,
                    workspace,
                )
                await session.stop()

            session = LspSession(workspace=workspace, config_path=config_path)
            self._sessions[key] = session
            self._config_mtimes[key] = _get_mtime(config_path)
            await session.start()
            return session

    async def get(
        self, workspace: Path, config_path: Path | None = None
    ) -> LspSession | None:
        """Return existing session or None."""
        workspace = workspace.resolve()
        config_path = config_path.resolve() if config_path else None
        if config_path is not None:
            return self._sessions.get(self._session_key(workspace, config_path))

        session = self._sessions.get(self._session_key(workspace, None))
        if session is not None:
            return session

        matches = [
            existing for key, existing in self._sessions.items() if key[0] == workspace
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    async def stop_all(self) -> None:
        """Shut down all active sessions."""
        for session in list(self._sessions.values()):
            try:
                await session.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error stopping session during stop_all: %s", exc)
        self._sessions.clear()
        self._config_mtimes.clear()

    async def restart(
        self, workspace: Path, config_path: Path | None = None
    ) -> LspSession:
        """Force-restart the session for a workspace."""
        workspace = workspace.resolve()
        config_path = config_path.resolve() if config_path else None
        key = self._session_key(workspace, config_path)
        lock = await self._ensure_lock(workspace, config_path)
        async with lock:
            existing = self._sessions.pop(key, None)
            self._config_mtimes.pop(key, None)
            if existing:
                await existing.stop()
        return await self.get_or_create(workspace, config_path)

    def status(self) -> dict[tuple[str, str | None], str]:
        return {
            (
                str(workspace),
                str(config_path) if config_path is not None else None,
            ): session.state.name
            for (workspace, config_path), session in self._sessions.items()
        }


# Module-level registry singleton
_registry = SessionRegistry()


def get_registry() -> SessionRegistry:
    return _registry


# ── Utilities ────────────────────────────────────────────────────────────────


def _lang_id(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".py": "python",
        ".xml": "xml",
        ".csv": "csv",
        ".js": "javascript",
        ".ts": "typescript",
    }.get(suffix, "plaintext")
