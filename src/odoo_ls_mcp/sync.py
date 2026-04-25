from __future__ import annotations

import logging
from pathlib import Path

from .diagnostics_cache import DiagnosticsCache
from .lsp_client import LspClient
from .utils import path_to_uri

logger = logging.getLogger(__name__)


def _lang_id(path: Path) -> str:
    return {
        ".py": "python",
        ".xml": "xml",
        ".csv": "csv",
        ".js": "javascript",
        ".ts": "typescript",
    }.get(path.suffix.lower(), "plaintext")


class DocumentSync:
    def __init__(self, client: LspClient, cache: DiagnosticsCache) -> None:
        self._client = client
        self._cache = cache
        self._open_uris: set[str] = set()
        self._versions: dict[str, int] = {}
        self._contents: dict[str, str] = {}

    async def open_file(self, file_path: Path) -> None:
        uri = path_to_uri(file_path)
        if uri in self._open_uris:
            return
        text = file_path.read_text(encoding="utf-8", errors="replace")
        self._versions[uri] = 1
        self._contents[uri] = text
        self._open_uris.add(uri)
        await self._client.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": _lang_id(file_path),
                    "version": 1,
                    "text": text,
                }
            },
        )
        logger.debug("didOpen %s", uri)

    async def close_file(self, file_path: Path) -> None:
        uri = path_to_uri(file_path)
        if uri not in self._open_uris:
            return
        self._open_uris.discard(uri)
        self._versions.pop(uri, None)
        self._contents.pop(uri, None)
        await self._client.notify(
            "textDocument/didClose",
            {"textDocument": {"uri": uri}},
        )
        logger.debug("didClose %s", uri)

    async def sync_file(self, file_path: Path) -> None:
        uri = path_to_uri(file_path)
        if uri not in self._open_uris:
            await self.open_file(file_path)
            return
        text = file_path.read_text(encoding="utf-8", errors="replace")
        if text == self._contents.get(uri):
            return
        version = self._versions.get(uri, 1) + 1
        self._versions[uri] = version
        self._contents[uri] = text
        await self._client.notify(
            "textDocument/didChange",
            {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": text}],
            },
        )
        logger.debug("didChange %s (v%d)", uri, version)

    def register_diagnostics_callback(self) -> None:
        self._client.on_notification(
            "textDocument/publishDiagnostics",
            self._on_publish_diagnostics,
        )

    def _on_publish_diagnostics(self, params: dict) -> None:
        uri = params.get("uri", "")
        diagnostics = params.get("diagnostics", [])
        self._cache.update(uri, diagnostics)

    @property
    def open_files(self) -> frozenset[str]:
        return frozenset(self._open_uris)
