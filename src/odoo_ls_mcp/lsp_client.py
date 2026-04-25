"""
Low-level async LSP stdio transport wrapping a single odoo_ls_server subprocess.

This module provides LspClient — a thin, testable transport layer that:
  - Spawns the OdooLS binary with asyncio stdin/stdout pipes
  - Performs the LSP initialize / initialized handshake
  - Routes responses back to callers via asyncio.Future (keyed by request id)
  - Drains stderr to Python logging (never to stdout)
  - Shuts down cleanly: shutdown request → exit notification → kill fallback

IMPORTANT: stdout is NEVER touched here; all log output → stderr via logging.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Callable
from typing import Any

from .config import WorkspaceConfig
from .lsp_session import LspSession, SessionState

logger = logging.getLogger(__name__)


class LspClient:
    """
    Low-level async LSP stdio transport wrapping a subprocess.

    Wraps LspSession to expose the interface expected by OdooLsSessionManager.
    Each LspClient manages exactly one OdooLS subprocess for one workspace.
    """

    def __init__(self) -> None:
        self._session: LspSession | None = None
        self._config: WorkspaceConfig | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self, config: WorkspaceConfig) -> None:
        """
        Spawn odoo_ls_server and perform the LSP initialize/initialized handshake.

        Args:
            config: Resolved workspace configuration including binary path,
                    workspace root, and optional config file path.

        Raises:
            FileNotFoundError: Binary not found or not executable.
            TimeoutError: Initialize handshake did not complete in time.
            RuntimeError: Any other startup failure.
        """
        self._config = config
        self._session = LspSession(
            workspace=config.workspace_root,
            config_path=config.config_path,
            binary=str(config.odools_binary),
        )
        await self._session.start()
        logger.debug(
            "LspClient started (workspace=%s, binary=%s)",
            config.workspace_root,
            config.odools_binary,
        )

    async def stop(self) -> None:
        """
        Graceful shutdown: send LSP shutdown request, send exit notification,
        then kill the subprocess if it doesn't exit in time.
        """
        if self._session is not None:
            await self._session.stop()
            logger.debug("LspClient stopped")

    # ── Messaging ─────────────────────────────────────────────────────────────

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        """
        Send an LSP request and await the response.

        Args:
            method: LSP method name (e.g. "textDocument/hover").
            params: Request parameters dict.

        Returns:
            The LSP ``result`` value from the server response.

        Raises:
            RuntimeError: Session not alive or server returned an error.
            TimeoutError: Response not received within the session timeout.
        """
        if self._session is None or not self._session.is_ready:
            raise RuntimeError(
                f"LspClient is not ready (session={self._session}). Call start() first."
            )
        return await self._session.request(method, params)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        """
        Send an LSP notification (fire-and-forget, no response expected).

        Args:
            method: LSP notification method name.
            params: Notification parameters dict.
        """
        if self._session is not None:
            self._session.notify(method, params)

    def on_notification(self, method: str, handler: Callable[[dict], None]) -> None:
        """Register a handler for LSP push notifications with the given method."""
        if self._session is not None:
            self._session.on_notification(method, handler)

    # ── State ─────────────────────────────────────────────────────────────────

    @property
    def is_alive(self) -> bool:
        """True if the subprocess is running and the LSP session is initialised."""
        return self._session is not None and self._session.is_ready

    @property
    def session(self) -> LspSession | None:
        """The underlying LspSession, if started."""
        return self._session
