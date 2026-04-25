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
from pathlib import Path
from typing import Any

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
    addon_path: Path,
    community_path: Path,
    tracked: Path,
    output_file: Path,
    stdlib_path: Path | None = None,
) -> list[str]:
    """
    Build the odoo_ls_server -p command.

    Args:
        addon_path:     Parent directory that contains the addon(s).
        community_path: Odoo community source root.
        tracked:        Specific addon folder to analyse.
        output_file:    Path where OdooLS will write its JSON output.
        stdlib_path:    Optional path to typeshed stdlib stubs.
    """
    binary = _find_binary()
    if binary is None:
        raise FileNotFoundError(
            f"'{ODOOLS_BINARY}' not found on PATH. "
            "Install OdooLS and ensure it is on your PATH."
        )
    cmd = [
        binary,
        "-p",
        "-a",
        str(addon_path),
        "-c",
        str(community_path),
        "-t",
        str(tracked),
        "-o",
        str(output_file),
    ]
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


async def run_parse(
    addon_path: str | Path,
    community_path: str | Path,
    tracked_path: str | Path | None = None,
    stdlib_path: str | Path | None = None,
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
    addon_path = Path(addon_path).resolve()
    community_path = Path(community_path).resolve()
    tracked = Path(tracked_path).resolve() if tracked_path else addon_path
    stdlib = Path(stdlib_path).resolve() if stdlib_path else None

    if not addon_path.exists():
        raise ValueError(f"addon_path does not exist: {addon_path}")
    if not community_path.exists():
        raise ValueError(f"community_path does not exist: {community_path}")
    if not tracked.exists():
        raise ValueError(f"tracked_path does not exist: {tracked}")

    errors: list[str] = []
    diagnostics: list[Diagnostic] = []
    raw: dict[str, Any] = {}

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        output_file = Path(tmp.name)

    try:
        cmd = _build_command(addon_path, community_path, tracked, output_file, stdlib)
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
            output_text = output_file.read_text(
                encoding="utf-8", errors="replace"
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
        workspace=str(addon_path),
        files_analyzed=files_analyzed,
        diagnostics=diagnostics,
        errors=errors,
        raw=raw,
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
