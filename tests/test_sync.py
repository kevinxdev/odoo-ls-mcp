from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from odoo_ls_mcp.diagnostics_cache import DiagnosticsCache
from odoo_ls_mcp.sync import DocumentSync
from odoo_ls_mcp.utils import path_to_uri


@pytest.fixture
def cache() -> DiagnosticsCache:
    return DiagnosticsCache()


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock()
    client.notify = AsyncMock()
    client.on_notification = MagicMock()
    return client


@pytest.fixture
def sync(mock_client: MagicMock, cache: DiagnosticsCache) -> DocumentSync:
    return DocumentSync(mock_client, cache)


@pytest.fixture
def py_file(tmp_path: Path) -> Path:
    f = tmp_path / "test_file.py"
    f.write_text("x = 1\n")
    return f


@pytest.mark.asyncio
async def test_open_file_sends_did_open(
    sync: DocumentSync, mock_client: MagicMock, py_file: Path
) -> None:
    await sync.open_file(py_file)

    mock_client.notify.assert_awaited_once()
    method, params = mock_client.notify.call_args[0]
    assert method == "textDocument/didOpen"
    assert params["textDocument"]["uri"] == path_to_uri(py_file)
    assert params["textDocument"]["text"] == "x = 1\n"
    assert params["textDocument"]["languageId"] == "python"
    assert params["textDocument"]["version"] == 1


@pytest.mark.asyncio
async def test_open_file_twice_sends_once(
    sync: DocumentSync, mock_client: MagicMock, py_file: Path
) -> None:
    await sync.open_file(py_file)
    await sync.open_file(py_file)

    assert mock_client.notify.await_count == 1


@pytest.mark.asyncio
async def test_open_file_adds_to_open_files(sync: DocumentSync, py_file: Path) -> None:
    assert path_to_uri(py_file) not in sync.open_files
    await sync.open_file(py_file)
    assert path_to_uri(py_file) in sync.open_files


@pytest.mark.asyncio
async def test_close_file_sends_did_close(
    sync: DocumentSync, mock_client: MagicMock, py_file: Path
) -> None:
    await sync.open_file(py_file)
    mock_client.notify.reset_mock()

    await sync.close_file(py_file)

    mock_client.notify.assert_awaited_once()
    method, params = mock_client.notify.call_args[0]
    assert method == "textDocument/didClose"
    assert params["textDocument"]["uri"] == path_to_uri(py_file)


@pytest.mark.asyncio
async def test_close_file_removes_from_open_set(
    sync: DocumentSync, py_file: Path
) -> None:
    await sync.open_file(py_file)
    await sync.close_file(py_file)
    assert path_to_uri(py_file) not in sync.open_files


@pytest.mark.asyncio
async def test_close_file_not_open_is_noop(
    sync: DocumentSync, mock_client: MagicMock, py_file: Path
) -> None:
    await sync.close_file(py_file)
    mock_client.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_file_opens_if_not_open(
    sync: DocumentSync, mock_client: MagicMock, py_file: Path
) -> None:
    await sync.sync_file(py_file)

    mock_client.notify.assert_awaited_once()
    method, _ = mock_client.notify.call_args[0]
    assert method == "textDocument/didOpen"


@pytest.mark.asyncio
async def test_sync_file_sends_did_change_on_content_change(
    sync: DocumentSync, mock_client: MagicMock, py_file: Path
) -> None:
    await sync.open_file(py_file)
    mock_client.notify.reset_mock()

    py_file.write_text("x = 2\n")
    await sync.sync_file(py_file)

    mock_client.notify.assert_awaited_once()
    method, params = mock_client.notify.call_args[0]
    assert method == "textDocument/didChange"
    assert params["contentChanges"][0]["text"] == "x = 2\n"
    assert params["textDocument"]["version"] == 2


@pytest.mark.asyncio
async def test_sync_file_no_change_no_notification(
    sync: DocumentSync, mock_client: MagicMock, py_file: Path
) -> None:
    await sync.open_file(py_file)
    mock_client.notify.reset_mock()

    await sync.sync_file(py_file)

    mock_client.notify.assert_not_awaited()


def test_register_diagnostics_callback(
    sync: DocumentSync, mock_client: MagicMock
) -> None:
    sync.register_diagnostics_callback()
    mock_client.on_notification.assert_called_once_with(
        "textDocument/publishDiagnostics", sync._on_publish_diagnostics
    )


def test_publish_diagnostics_updates_cache(
    sync: DocumentSync, cache: DiagnosticsCache
) -> None:
    uri = "file:///workspace/models/res_partner.py"
    params = {
        "uri": uri,
        "diagnostics": [
            {
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 10},
                },
                "severity": 1,
                "message": "Cannot find symbol 'X'",
                "source": "odoo_ls",
            }
        ],
    }

    sync._on_publish_diagnostics(params)

    diags = cache.get(uri)
    assert len(diags) == 1
    d = diags[0]
    assert d.uri == uri
    assert d.severity == 1
    assert d.message == "Cannot find symbol 'X'"
    assert d.source == "odoo_ls"
    assert d.range_start_line == 0
    assert d.range_end_char == 10


def test_diagnostics_cache_update_replaces(cache: DiagnosticsCache) -> None:
    uri = "file:///a.py"
    cache.update(
        uri,
        [
            {
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 5},
                },
                "severity": 2,
                "message": "warn1",
                "source": None,
            }
        ],
    )
    cache.update(
        uri,
        [
            {
                "range": {
                    "start": {"line": 1, "character": 0},
                    "end": {"line": 1, "character": 3},
                },
                "severity": 1,
                "message": "err1",
                "source": "odoo_ls",
            }
        ],
    )

    diags = cache.get(uri)
    assert len(diags) == 1
    assert diags[0].message == "err1"


def test_diagnostics_cache_get_unknown_uri(cache: DiagnosticsCache) -> None:
    assert cache.get("file:///nonexistent.py") == []


def test_diagnostics_cache_get_all(cache: DiagnosticsCache) -> None:
    cache.update("file:///a.py", [])
    cache.update("file:///b.py", [])
    result = cache.get_all()
    assert set(result.keys()) == {"file:///a.py", "file:///b.py"}


def test_diagnostics_cache_clear_single(cache: DiagnosticsCache) -> None:
    cache.update("file:///a.py", [])
    cache.update("file:///b.py", [])
    cache.clear("file:///a.py")
    assert cache.get("file:///a.py") == []
    assert "file:///b.py" in cache.get_all()


def test_diagnostics_cache_clear_all(cache: DiagnosticsCache) -> None:
    cache.update("file:///a.py", [])
    cache.clear()
    assert cache.get_all() == {}
