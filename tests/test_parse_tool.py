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
    DEFAULT_STDLIB_PATH,
    _parse_output,
    format_diagnostics_text,
    run_parse,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

SAMPLE_EVENTS_OUTPUT = {
    "events": [
        {"type": "log", "severity": 3, "message": "Adding sys.path: /some/path"},
        {
            "type": "diagnostic",
            "uri": "file:///workspace/addon/__manifest__.py",
            "version": None,
            "diagnostics": [
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
        },
        {
            "type": "diagnostic",
            "uri": "file:///workspace/addon/models/res_partner.py",
            "version": None,
            "diagnostics": [
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
        },
    ]
}


@pytest.fixture
def tmp_addon(tmp_path: Path) -> tuple[Path, Path]:
    """Return (addon_parent, community_path) — both exist."""
    addon = tmp_path / "my_addon"
    addon.mkdir()
    (addon / "__manifest__.py").write_text('{"name": "Test", "depends": ["base"]}')
    community = tmp_path / "odoo"
    community.mkdir()
    return tmp_path, community


# ---------------------------------------------------------------------------
# Unit: _parse_output
# ---------------------------------------------------------------------------


def test_parse_output_basic():
    result = _parse_output(SAMPLE_EVENTS_OUTPUT)
    assert len(result) == 3


def test_parse_output_only_diagnostic_events():
    """Log events must be ignored."""
    result = _parse_output(SAMPLE_EVENTS_OUTPUT)
    files = {d.file for d in result}
    # No log-event leakage
    assert all("manifest" in f or "res_partner" in f for f in files)


def test_parse_output_uri_to_path():
    result = _parse_output(SAMPLE_EVENTS_OUTPUT)
    paths = {d.file for d in result}
    assert "/workspace/addon/__manifest__.py" in paths
    assert "file:///workspace/addon/__manifest__.py" not in paths


def test_parse_output_severity_values():
    result = _parse_output(SAMPLE_EVENTS_OUTPUT)
    errors = [d for d in result if d.severity == DiagnosticSeverity.ERROR]
    warnings = [d for d in result if d.severity == DiagnosticSeverity.WARNING]
    assert len(errors) == 2
    assert len(warnings) == 1


def test_parse_output_empty_events():
    assert _parse_output({"events": []}) == []


def test_parse_output_missing_events_key():
    assert _parse_output({}) == []


def test_parse_output_malformed_diagnostic_skipped():
    """A None diagnostic entry must be skipped without raising."""
    raw = {
        "events": [
            {
                "type": "diagnostic",
                "uri": "file:///workspace/foo.py",
                "diagnostics": [
                    None,  # malformed
                    {
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 0},
                        },
                        "severity": 1,
                        "message": "ok",
                    },
                ],
            }
        ]
    }
    result = _parse_output(raw)
    assert len(result) == 1
    assert result[0].message == "ok"


def test_parse_output_non_file_uri():
    """URIs that don't start with file:// are kept as-is."""
    raw = {
        "events": [
            {
                "type": "diagnostic",
                "uri": "/absolute/path/file.py",
                "diagnostics": [
                    {
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 0},
                        },
                        "severity": 1,
                        "message": "err",
                    }
                ],
            }
        ]
    }
    result = _parse_output(raw)
    assert result[0].file == "/absolute/path/file.py"


# ---------------------------------------------------------------------------
# Unit: format_diagnostics_text
# ---------------------------------------------------------------------------


def _make_result(n_errors: int = 2, n_warnings: int = 1) -> ParseResult:
    diags = []
    for i in range(n_errors):
        diags.append(
            Diagnostic(
                file=f"/workspace/file{i}.py",
                range=Range(
                    start=Position(line=i, character=0),
                    end=Position(line=i, character=5),
                ),
                severity=DiagnosticSeverity.ERROR,
                code=f"OLS0300{i}",
                message=f"Error {i}",
            )
        )
    for i in range(n_warnings):
        diags.append(
            Diagnostic(
                file="/workspace/manifest.py",
                range=Range(
                    start=Position(line=i, character=0),
                    end=Position(line=i, character=5),
                ),
                severity=DiagnosticSeverity.WARNING,
                code=f"OLS0400{i}",
                message=f"Warning {i}",
            )
        )
    return ParseResult(workspace="/workspace", files_analyzed=5, diagnostics=diags)


def test_format_no_diagnostics():
    result = ParseResult(workspace="/ws", files_analyzed=3, diagnostics=[])
    text = format_diagnostics_text(result)
    assert "No diagnostics" in text
    assert "3 file" in text


def test_format_with_diagnostics():
    result = _make_result()
    text = format_diagnostics_text(result)
    assert "🔴" in text
    assert "🟡" in text
    assert "OLS03000" in text
    assert "3 issue" in text


def test_format_with_errors():
    result = ParseResult(
        workspace="/ws", files_analyzed=0, diagnostics=[], errors=["binary not found"]
    )
    text = format_diagnostics_text(result)
    assert "binary not found" in text


# ---------------------------------------------------------------------------
# run_parse with mocked subprocess + temp output file
# ---------------------------------------------------------------------------


def _make_mock_proc(output_data: dict[str, object], returncode: int = 0) -> MagicMock:
    """Create a mock subprocess that writes output_data to the temp file."""
    mock_proc = MagicMock()
    mock_proc.returncode = returncode

    async def fake_communicate():
        return b"", b""

    mock_proc.communicate = fake_communicate
    mock_proc._output_data = output_data
    return mock_proc


@pytest.fixture
def mock_binary():
    with patch(
        "odoo_ls_mcp.parse_tool._find_binary", return_value="/usr/bin/odoo_ls_server"
    ):
        yield


@pytest.mark.asyncio
async def test_run_parse_binary_not_found(tmp_addon):
    addon_parent, community = tmp_addon
    with patch("odoo_ls_mcp.parse_tool._find_binary", return_value=None):
        with pytest.raises(FileNotFoundError, match="not found on PATH"):
            await run_parse(addon_parent, community)


@pytest.mark.asyncio
async def test_run_parse_invalid_addon_path(tmp_addon):
    _, community = tmp_addon
    with pytest.raises(ValueError, match="does not exist"):
        await run_parse("/nonexistent/addon/path", community)


@pytest.mark.asyncio
async def test_run_parse_invalid_community_path(tmp_addon):
    addon_parent, _ = tmp_addon
    with pytest.raises(ValueError, match="does not exist"):
        await run_parse(addon_parent, "/nonexistent/community")


@pytest.mark.asyncio
async def test_run_parse_success(tmp_addon, mock_binary):
    """Successful run: output file is written, diagnostics are parsed."""
    addon_parent, community = tmp_addon
    output_json = json.dumps(SAMPLE_EVENTS_OUTPUT).encode()

    mock_proc = MagicMock()
    mock_proc.returncode = 0

    async def fake_communicate():
        return b"", b""

    mock_proc.communicate = fake_communicate

    async def fake_exec(*args, **kwargs):
        # Write the fake output to the -o file argument
        cmd = list(args)
        o_idx = cmd.index("-o")
        out_path = Path(cmd[o_idx + 1])
        out_path.write_bytes(output_json)
        return mock_proc

    with patch("asyncio.create_subprocess_exec", new=fake_exec):
        result = await run_parse(
            addon_parent, community, min_severity=DiagnosticSeverity.HINT
        )

    assert len(result.diagnostics) == 3
    assert result.errors == []
    # 2 unique files had diagnostics
    assert result.files_analyzed == 2


@pytest.mark.asyncio
async def test_run_parse_workspace_config_mode(tmp_path, mock_binary):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    addons_a = workspace / "addons_a"
    addons_b = workspace / "addons_b"
    addons_a.mkdir()
    addons_b.mkdir()
    community = workspace / "odoo"
    community.mkdir()
    config = workspace / "odools.toml"
    config.write_text(
        "\n".join(
            [
                "[[config]]",
                'odoo_path = "./odoo"',
                'addons_paths = ["./addons_a", "./addons_b", "$autoDetectAddons"]',
                'python_path = "python3"',
                f'stdlib = "{DEFAULT_STDLIB_PATH.as_posix()}"',
            ]
        )
    )

    output_json = json.dumps(SAMPLE_EVENTS_OUTPUT).encode()
    mock_proc = MagicMock()
    mock_proc.returncode = 0

    async def fake_communicate():
        return b"", b""

    mock_proc.communicate = fake_communicate

    async def fake_exec(*args, **kwargs):
        cmd = list(args)
        assert cmd.count("-a") == 2
        assert "--python" in cmd
        assert cmd[cmd.index("-c") + 1] == str(community.resolve())
        assert cmd[cmd.index("-t") + 1] == str(workspace.resolve())
        out_path = Path(cmd[cmd.index("-o") + 1])
        out_path.write_bytes(output_json)
        return mock_proc

    with patch("asyncio.create_subprocess_exec", new=fake_exec):
        result = await run_parse(workspace=workspace, config_path=config)

    assert len(result.diagnostics) == 3
    assert result.workspace == str(workspace.resolve())


@pytest.mark.asyncio
async def test_run_parse_severity_filter(tmp_addon, mock_binary):
    """min_severity=ERROR should only return severity-1 entries."""
    addon_parent, community = tmp_addon
    output_json = json.dumps(SAMPLE_EVENTS_OUTPUT).encode()

    mock_proc = MagicMock()
    mock_proc.returncode = 0

    async def fake_communicate():
        return b"", b""

    mock_proc.communicate = fake_communicate

    async def fake_exec(*args, **kwargs):
        cmd = list(args)
        o_idx = cmd.index("-o")
        Path(cmd[o_idx + 1]).write_bytes(output_json)
        return mock_proc

    with patch("asyncio.create_subprocess_exec", new=fake_exec):
        result = await run_parse(
            addon_parent, community, min_severity=DiagnosticSeverity.ERROR
        )

    assert all(d.severity == DiagnosticSeverity.ERROR for d in result.diagnostics)


@pytest.mark.asyncio
async def test_run_parse_nonzero_exit(tmp_addon, mock_binary):
    addon_parent, community = tmp_addon

    mock_proc = MagicMock()
    mock_proc.returncode = 1

    async def fake_communicate():
        return b"", b"Config not found"

    mock_proc.communicate = fake_communicate

    async def fake_exec(*args, **kwargs):
        return mock_proc

    with patch("asyncio.create_subprocess_exec", new=fake_exec):
        result = await run_parse(addon_parent, community)

    assert len(result.errors) == 1
    assert "exited with code 1" in result.errors[0]


@pytest.mark.asyncio
async def test_run_parse_timeout(tmp_addon, mock_binary):
    """TimeoutError is raised and contains 'timed out'."""
    addon_parent, community = tmp_addon

    mock_proc = MagicMock()
    mock_proc.kill = MagicMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    async def fake_exec(*args, **kwargs):
        return mock_proc

    async def fake_wait_for(coro, timeout):
        coro.close()
        raise TimeoutError()

    with patch("asyncio.create_subprocess_exec", new=fake_exec):
        with patch("asyncio.wait_for", new=fake_wait_for):
            with pytest.raises(TimeoutError, match="timed out"):
                await run_parse(addon_parent, community, timeout=1)

    mock_proc.kill.assert_called_once()
