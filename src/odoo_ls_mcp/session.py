"""
Session lifecycle manager for odoo-ls-mcp.

Manages one LspClient per canonical workspace/config key. Responsibilities:
  - Lazy start: create a session on first get_session() call for a workspace
  - Reuse: return the same LspClient on subsequent calls for the same key
  - Transparent restart: if the subprocess crashes, recreate automatically
  - Concurrent-safe: per-key asyncio.Lock prevents duplicate startup races
  - Idle TTL: background task stops sessions after cfg.idle_ttl_s of inactivity

IMPORTANT: stdout is never used; all logging → stderr.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .config import WorkspaceConfig, session_key
from .lsp_client import LspClient

logger = logging.getLogger(__name__)

SessionKey = tuple[str, str | None]


class OdooLsSessionManager:
    """
    Manages one LspClient per canonical workspace/config key.

    Thread-safety: designed for use from a single asyncio event loop.
    All public methods are coroutines that must be awaited.
    """

    def __init__(self) -> None:
        self._sessions: dict[SessionKey, LspClient] = {}
        self._locks: dict[SessionKey, asyncio.Lock] = {}
        self._timers: dict[SessionKey, asyncio.Task[Any]] = {}
        self._global_lock = asyncio.Lock()

    async def get_session(self, cfg: WorkspaceConfig) -> LspClient:
        """
        Return a live LspClient for this workspace (create if needed, reuse if alive).

        Transparent restart: if the cached session's subprocess has crashed
        (is_alive=False), it is stopped and a fresh one is created automatically.

        The idle TTL timer is reset on every call, so sessions stay alive as long
        as they are being used.

        Args:
            cfg: Resolved workspace configuration.

        Returns:
            A started, alive LspClient for this workspace.
        """
        key = session_key(cfg)

        async with self._global_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()

        async with self._locks[key]:
            client = self._sessions.get(key)

            if client is not None and not client.is_alive:
                logger.info("Cached session is dead, restarting (key=%s)", key)
                await client.stop()
                client = None

            if client is None:
                client = LspClient()
                await client.start(cfg)
                self._sessions[key] = client
                logger.info("Created new LSP session (key=%s)", key)

            self._reset_idle_timer(key, cfg)
            return client

    async def restart_session(self, cfg: WorkspaceConfig) -> LspClient:
        """
        Force-kill the existing session (if any) and start a fresh one.

        Useful when the workspace configuration has changed or when the
        caller suspects the session is in a bad state.

        Args:
            cfg: Resolved workspace configuration.

        Returns:
            A freshly started LspClient.
        """
        key = session_key(cfg)

        async with self._global_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()

        async with self._locks[key]:
            self._cancel_timer(key)
            old = self._sessions.pop(key, None)
            if old is not None:
                await old.stop()

            client = LspClient()
            await client.start(cfg)
            self._sessions[key] = client
            self._reset_idle_timer(key, cfg)
            logger.info("Restarted LSP session (key=%s)", key)
            return client

    async def close_all(self) -> None:
        """
        Shut down all active sessions cleanly.

        Cancels all idle timers and calls stop() on every LspClient.
        Safe to call multiple times; subsequent calls are no-ops.
        """
        keys = list(self._sessions.keys())
        for key in keys:
            self._cancel_timer(key)
            client = self._sessions.pop(key, None)
            if client is not None:
                try:
                    await client.stop()
                except Exception as exc:
                    logger.warning("Error stopping session %s: %s", key, exc)
        logger.info("All LSP sessions closed")

    def _cancel_timer(self, key: SessionKey) -> None:
        timer = self._timers.pop(key, None)
        if timer is not None and not timer.done():
            timer.cancel()

    def _reset_idle_timer(self, key: SessionKey, cfg: WorkspaceConfig) -> None:
        self._cancel_timer(key)
        ttl = cfg.idle_ttl_s

        async def _expire() -> None:
            await asyncio.sleep(ttl)
            client = self._sessions.pop(key, None)
            if client is not None:
                logger.info(
                    "Idle TTL (%.0fs) expired for session (key=%s), stopping",
                    ttl,
                    key,
                )
                try:
                    await client.stop()
                except Exception as exc:
                    logger.warning("Error during idle stop %s: %s", key, exc)

        self._timers[key] = asyncio.create_task(_expire(), name=f"idle-ttl-{key}")
