"""
odoo-ls-mcp: --parse mode tool.

Invokes `odoo_ls_server -p` as a one-shot subprocess.
OdooLS writes a JSON diagnostics report to a temp file then exits.

stdout is never touched by this module — all logging goes to stderr.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from .config import resolve_config
from .models import Diagnostic, DiagnosticSeverity, ParseResult, Position, Range

logger = logging.getLogger(__name__)

# OdooLS binary name (stable across distributions per research)
ODOOLS_BINARY = "odoo_ls_server"

# Default timeout for --parse mode (large workspaces can be slow)
DEFAULT_PARSE_TIMEOUT_S = 120

# Default stdlib stubs path (shipped with OdooLS)
DEFAULT_STDLIB_PATH = Path.home() / ".local/share/odoo-ls/typeshed/stdlib"


def _find_binary() -> str | None:
    """Return the path to odoo_ls_server, or None if not found."""
    return shutil.which(ODOOLS_BINARY)


def _build_command(
    addon_paths: list[Path],
    community_path: Path,
    tracked: Path,
    output_file: Path,
    python_path: str | None = None,
    stdlib_path: Path | None = None,
) -> list[str]:
    binary = _find_binary()
    if binary is None:
        raise FileNotFoundError(
            f"'{ODOOLS_BINARY}' not found on PATH. "
            "Install OdooLS and ensure it is on your PATH."
        )
    cmd = [
        binary,
        "-p",
        "-c",
        str(community_path),
        "-t",
        str(tracked),
        "-o",
        str(output_file),
    ]
    for addon_path in addon_paths:
        cmd += ["-a", str(addon_path)]
    if python_path:
        cmd += ["--python", python_path]
    effective_stdlib = stdlib_path or (
        DEFAULT_STDLIB_PATH if DEFAULT_STDLIB_PATH.exists() else None
    )
    if effective_stdlib is not None:
        cmd += ["--stdlib", str(effective_stdlib)]
    return cmd


def _parse_output(data: dict[str, Any]) -> list[Diagnostic]:
    """
    Parse the {"events": [...]} JSON written by odoo_ls_server -p.

    Only events with "type": "diagnostic" are processed.
    Each such event has a "uri" and a "diagnostics" list of standard LSP objects.
    """
    results: list[Diagnostic] = []
    for event in data.get("events", []):
        if event.get("type") != "diagnostic":
            continue
        uri = event.get("uri", "")
        file_path = uri[7:] if uri.startswith("file://") else uri
        for d in event.get("diagnostics", []):
            try:
                r = d.get("range", {})
                start = r.get("start", {})
                end = r.get("end", {})
                results.append(
                    Diagnostic(
                        file=file_path,
                        range=Range(
                            start=Position(
                                line=start.get("line", 0),
                                character=start.get("character", 0),
                            ),
                            end=Position(
                                line=end.get("line", 0),
                                character=end.get("character", 0),
                            ),
                        ),
                        severity=d.get("severity", DiagnosticSeverity.ERROR),
                        code=d.get("code"),
                        source=d.get("source"),
                        message=d.get("message", ""),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Skipping malformed diagnostic entry: %s — %s", d, exc)
    return results


def _parse_lsp_diagnostics(data: dict[str, Any], workspace: str) -> list[Diagnostic]:
    return _parse_output(data)


def _resolve_path(base_dir: Path, value: str) -> Path | None:
    if value == "$autoDetectAddons":
        return None
    path = Path(value)
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    else:
        path = path.resolve()
    return path


def _parse_workspace_config(
    workspace: str | Path, config_path: str | Path | None = None
) -> tuple[Path, list[Path], Path, str | None, Path | None]:
    cfg = resolve_config(workspace_root=workspace, config_path=config_path)
    assert cfg.config_path is not None

    raw = tomllib.loads(Path(cfg.config_path).read_text(encoding="utf-8"))
    entries = raw.get("config")
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"No [[config]] entries found in {cfg.config_path}")

    entry = entries[0]
    if not isinstance(entry, dict):
        raise ValueError(f"Invalid [[config]] entry in {cfg.config_path}")

    config_dir = cfg.config_path.parent
    odoo_value = entry.get("odoo_path")
    if not isinstance(odoo_value, str) or not odoo_value.strip():
        raise ValueError(f"Config {cfg.config_path} is missing a valid odoo_path")
    community_path = _resolve_path(config_dir, odoo_value)
    assert community_path is not None

    addons_value = entry.get("addons_paths")
    if not isinstance(addons_value, list) or not addons_value:
        raise ValueError(f"Config {cfg.config_path} is missing valid addons_paths")
    addon_paths = [
        resolved
        for item in addons_value
        if isinstance(item, str)
        for resolved in [_resolve_path(config_dir, item)]
        if resolved is not None
    ]
    if not addon_paths:
        raise ValueError(f"Config {cfg.config_path} did not resolve any addon paths")

    python_value = entry.get("python_path")
    python_path = (
        python_value if isinstance(python_value, str) and python_value else None
    )

    stdlib_value = entry.get("stdlib")
    stdlib_path = (
        _resolve_path(config_dir, stdlib_value)
        if isinstance(stdlib_value, str) and stdlib_value
        else None
    )

    return cfg.workspace_root, addon_paths, community_path, python_path, stdlib_path


async def run_parse(
    addon_path: str | Path | None = None,
    community_path: str | Path | None = None,
    tracked_path: str | Path | None = None,
    stdlib_path: str | Path | None = None,
    workspace: str | Path | None = None,
    config_path: str | Path | None = None,
    timeout: float = DEFAULT_PARSE_TIMEOUT_S,
    min_severity: int = DiagnosticSeverity.HINT,
) -> ParseResult:
    """
    Run ``odoo_ls_server -p`` and return structured diagnostics.

    Args:
        addon_path:     Parent directory that contains the addon(s) to analyse.
        community_path: Odoo community source root (the ``odoo`` package dir).
        tracked_path:   Specific addon to track; defaults to addon_path itself.
        stdlib_path:    Optional path to typeshed stdlib stubs.
        timeout:        Seconds to wait before aborting (default 120).
        min_severity:   Only return diagnostics at or above this level.
                        1=error, 2=warning, 3=info, 4=hint (default: all).

    Returns:
        ParseResult with diagnostics list and metadata.
    """
    python_path: str | None = None
    if workspace is not None:
        (
            workspace_root,
            addon_paths,
            community_root,
            config_python,
            config_stdlib,
        ) = await asyncio.to_thread(_parse_workspace_config, workspace, config_path)
        tracked = Path(tracked_path).resolve() if tracked_path else workspace_root
        stdlib = Path(stdlib_path).resolve() if stdlib_path else config_stdlib
        python_path = config_python
        result_workspace = workspace_root
    else:
        if addon_path is None or community_path is None:
            raise ValueError(
                "run_parse requires either workspace/config_path or addon_path/community_path"
            )
        resolved_addon_path = Path(addon_path).resolve()
        addon_paths = [resolved_addon_path]
        community_root = Path(community_path).resolve()
        tracked = Path(tracked_path).resolve() if tracked_path else resolved_addon_path
        stdlib = Path(stdlib_path).resolve() if stdlib_path else None
        result_workspace = resolved_addon_path

    for addon_dir in addon_paths:
        if not addon_dir.exists():
            raise ValueError(f"addon_path does not exist: {addon_dir}")
    if not community_root.exists():
        raise ValueError(f"community_path does not exist: {community_root}")
    if not tracked.exists():
        raise ValueError(f"tracked_path does not exist: {tracked}")

    errors: list[str] = []
    diagnostics: list[Diagnostic] = []
    raw: dict[str, Any] = {}

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        output_file = Path(tmp.name)

    try:
        cmd = _build_command(
            addon_paths,
            community_root,
            tracked,
            output_file,
            python_path=python_path,
            stdlib_path=stdlib,
        )
        logger.debug("Running: %s", " ".join(cmd))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            stdin=asyncio.subprocess.DEVNULL,
        )

        try:
            _, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            raise TimeoutError(  # noqa: B904
                f"odoo_ls_server -p timed out after {timeout}s. "
                "Consider increasing the timeout for large workspaces."
            )

        stderr_text = stderr_bytes.decode(errors="replace").strip()
        if stderr_text:
            for line in stderr_text.splitlines():
                logger.debug("[odoo_ls_server stderr] %s", line)

        if proc.returncode != 0:
            errors.append(
                f"odoo_ls_server exited with code {proc.returncode}. "
                f"stderr: {stderr_text[:500]}"
            )
            logger.warning(
                "odoo_ls_server -p exited %d: %s", proc.returncode, stderr_text[:200]
            )
        elif output_file.exists():
            output_text = (
                await asyncio.to_thread(
                    output_file.read_text, encoding="utf-8", errors="replace"
                )
            ).strip()
            if output_text:
                try:
                    raw = json.loads(output_text)
                    diagnostics = _parse_output(raw)
                except json.JSONDecodeError as exc:
                    errors.append(
                        f"Failed to parse JSON output from odoo_ls_server: {exc}"
                    )
                    logger.error(
                        "JSON decode error: %s\nOutput was: %.500s", exc, output_text
                    )
    finally:
        try:
            output_file.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to clean up temp output file: %s", exc)

    # Apply severity filter (lower number = higher severity)
    if min_severity < DiagnosticSeverity.HINT:
        diagnostics = [d for d in diagnostics if d.severity <= min_severity]

    # Count unique files that had diagnostics
    files_analyzed = len({d.file for d in diagnostics})

    return ParseResult(
        files_analyzed=files_analyzed,
        diagnostics=diagnostics,
        errors=errors,
        raw=raw,
        workspace=str(result_workspace),
    )


def format_diagnostics_text(result: ParseResult) -> str:
    """Format a ParseResult as a human-readable text summary."""
    lines: list[str] = []

    if result.errors:
        lines.append("⚠️  Errors during analysis:")
        for e in result.errors:
            lines.append(f"  • {e}")
        lines.append("")

    if not result.diagnostics:
        lines.append(
            f"✅ No diagnostics found in workspace: {result.workspace}"
            f" ({result.files_analyzed} file(s) analyzed)"
        )
        return "\n".join(lines)

    lines.append(
        f"📋 Diagnostics for workspace: {result.workspace}"
        f" ({result.files_analyzed} file(s) analyzed, "
        f"{len(result.diagnostics)} issue(s))"
    )
    lines.append("")

    # Group by file
    by_file: dict[str, list[Diagnostic]] = {}
    for d in sorted(result.diagnostics, key=lambda x: (x.file, x.range.start.line)):
        by_file.setdefault(d.file, []).append(d)

    severity_icons = {1: "🔴", 2: "🟡", 3: "🔵", 4: "⚪"}

    for file_path, diags in by_file.items():
        lines.append(f"📄 {file_path}  ({len(diags)} issue(s))")
        for d in diags:
            icon = severity_icons.get(d.severity, "•")
            loc = f"L{d.range.start.line + 1}:{d.range.start.character + 1}"
            code = f"[{d.code}] " if d.code else ""
            lines.append(f"  {icon} {loc}  {code}{d.message}")
        lines.append("")

    return "\n".join(lines)
