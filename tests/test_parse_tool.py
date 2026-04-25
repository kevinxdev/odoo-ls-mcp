"""Tests for the --parse mode tool (no real OdooLS required)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from odoo_ls_mcp.models import (
    Diagnostic,
    DiagnosticSeverity,
    ParseResult,
    Position,
    Range,
)
from odoo_ls_mcp.parse_tool import (
    _parse_lsp_diagnostics,
    format_diagnostics_text,
    run_parse,
)

# ---------------------------------------------------------------------------
# Unit: _parse_lsp_diagnostics
# ---------------------------------------------------------------------------

SAMPLE_LSP_OUTPUT = {
    "file:///workspace/addon/__manifest__.py": [
        {
            "range": {
                "start": {"line": 4, "character": 0},
                "end": {"line": 4, "character": 10},
            },
            "severity": 1,
            "code": "OLS04001",
            "source": "OdooLS",
            "message": "Missing required key 'version'",
        },
        {
            "range": {
                "start": {"line": 10, "character": 0},
                "end": {"line": 10, "character": 5},
            },
            "severity": 2,
            "code": "OLS04010",
            "source": "OdooLS",
            "message": "Deprecated key 'description' format",
        },
    ],
    "file:///workspace/addon/models/res_partner.py": [
        {
            "range": {
                "start": {"line": 12, "character": 4},
                "end": {"line": 12, "character": 30},
            },
            "severity": 1,
            "code": "OLS03001",
            "source": "OdooLS",
            "message": "Field 'partner_id' not found on model 'res.partner'",
        },
    ],
}


def test_parse_lsp_diagnostics_basic():
    result = _parse_lsp_diagnostics(SAMPLE_LSP_OUTPUT, "/workspace")
    assert len(result) == 3

    errors = [d for d in result if d.severity == DiagnosticSeverity.ERROR]
    warnings = [d for d in result if d.severity == DiagnosticSeverity.WARNING]
    assert len(errors) == 2
    assert len(warnings) == 1


def test_parse_lsp_diagnostics_uri_to_path():
    result = _parse_lsp_diagnostics(SAMPLE_LSP_OUTPUT, "/workspace")
    paths = {d.file for d in result}
    assert "/workspace/addon/__manifest__.py" in paths
    assert "file:///workspace/addon/__manifest__.py" not in paths


def test_parse_lsp_diagnostics_empty():
    assert _parse_lsp_diagnostics({}, "/workspace") == []


def test_parse_lsp_diagnostics_malformed_entry():
    """Malformed entries should be skipped, not raise."""
    raw = {
        "file:///workspace/foo.py": [
            None,  # malformed
            {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}, "severity": 1, "message": "ok"},
        ]
    }
    result = _parse_lsp_diagnostics(raw, "/workspace")
    assert len(result) == 1
    assert result[0].message == "ok"


# ---------------------------------------------------------------------------
# Unit: format_diagnostics_text
# ---------------------------------------------------------------------------

def _make_result(n_errors: int = 2, n_warnings: int = 1) -> ParseResult:
    diags = []
    for i in range(n_errors):
        diags.append(Diagnostic(
            file=f"/workspace/file{i}.py",
            range=Range(start=Position(line=i, character=0), end=Position(line=i, character=5)),
            severity=DiagnosticSeverity.ERROR,
            code=f"OLS0300{i}",
            message=f"Error {i}",
        ))
    for i in range(n_warnings):
        diags.append(Diagnostic(
            file="/workspace/manifest.py",
            range=Range(start=Position(line=i, character=0), end=Position(line=i, character=5)),
            severity=DiagnosticSeverity.WARNING,
            code=f"OLS0400{i}",
            message=f"Warning {i}",
        ))
    return ParseResult(workspace="/workspace", files_analyzed=5, diagnostics=diags)


def test_format_no_diagnostics():
    result = ParseResult(workspace="/ws", files_analyzed=3, diagnostics=[])
    text = format_diagnostics_text(result)
    assert "No diagnostics" in text
    assert "3 file" in text


def test_format_with_diagnostics():
    result = _make_result()
    text = format_diagnostics_text(result)
    assert "🔴" in text  # error icon
    assert "🟡" in text  # warning icon
    assert "OLS03000" in text
    assert "3 issue" in text


def test_format_with_errors():
    result = ParseResult(workspace="/ws", files_analyzed=0, diagnostics=[], errors=["binary not found"])
    text = format_diagnostics_text(result)
    assert "binary not found" in text


# ---------------------------------------------------------------------------
# Integration-ish: run_parse with mocked subprocess
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    (tmp_path / "odools.toml").write_text("[[config]]\nodoo_path = '/odoo'\n")
    return tmp_path


@pytest.mark.asyncio
async def test_run_parse_binary_not_found(tmp_workspace: Path):
    with patch("odoo_ls_mcp.parse_tool._find_binary", return_value=None):
        with pytest.raises(FileNotFoundError, match="not found on PATH"):
            await run_parse(tmp_workspace)


@pytest.mark.asyncio
async def test_run_parse_invalid_workspace():
    with pytest.raises(ValueError, match="does not exist"):
        await run_parse("/nonexistent/path/that/does/not/exist/at/all")


@pytest.mark.asyncio
async def test_run_parse_success(tmp_workspace: Path):
    """Simulate a successful --parse run returning LSP JSON."""
    output = json.dumps(SAMPLE_LSP_OUTPUT).encode()

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(output, b""))

    with patch("odoo_ls_mcp.parse_tool._find_binary", return_value="/usr/bin/odoo_ls_server"):
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
            result = await run_parse(tmp_workspace, min_severity=DiagnosticSeverity.HINT)

    assert result.files_analyzed == 2
    assert len(result.diagnostics) == 3
    assert result.errors == []


@pytest.mark.asyncio
async def test_run_parse_severity_filter(tmp_workspace: Path):
    """min_severity=1 should only return errors."""
    output = json.dumps(SAMPLE_LSP_OUTPUT).encode()

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(output, b""))

    with patch("odoo_ls_mcp.parse_tool._find_binary", return_value="/usr/bin/odoo_ls_server"):
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
            result = await run_parse(tmp_workspace, min_severity=DiagnosticSeverity.ERROR)

    # Only errors should survive the filter
    assert all(d.severity == DiagnosticSeverity.ERROR for d in result.diagnostics)


@pytest.mark.asyncio
async def test_run_parse_nonzero_exit(tmp_workspace: Path):
    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.communicate = AsyncMock(return_value=(b"", b"Config not found"))

    with patch("odoo_ls_mcp.parse_tool._find_binary", return_value="/usr/bin/odoo_ls_server"):
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
            result = await run_parse(tmp_workspace)

    assert len(result.errors) == 1
    assert "exited with code 1" in result.errors[0]




# ---------------------------------------------------------------------------
# Timeout test (rewritten — avoids side_effect ordering issue)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_parse_timeout_v2(tmp_workspace: Path):
    """Timeout raises our custom TimeoutError (with descriptive message)."""

    mock_proc = MagicMock()
    mock_proc.kill = MagicMock()
    # cleanup communicate() after kill always succeeds
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    async def fake_wait_for(coro, timeout):
        coro.close()  # drain to avoid "never awaited"
        raise TimeoutError()

    with patch("odoo_ls_mcp.parse_tool._find_binary", return_value="/usr/bin/odoo_ls_server"):
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
            with patch("asyncio.wait_for", new=fake_wait_for):
                with pytest.raises(TimeoutError, match="timed out"):
                    await run_parse(tmp_workspace, timeout=1)

    mock_proc.kill.assert_called_once()
