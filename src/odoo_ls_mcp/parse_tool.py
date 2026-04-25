"""
odoo-ls-mcp: --parse mode tool.

Invokes `odoo_ls_server --parse` as a one-shot subprocess.
This is the simplest integration path: no long-lived LSP session required.
OdooLS outputs a JSON diagnostics report then exits cleanly.

stdout is never touched by this module — all logging goes to stderr.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any

from .models import Diagnostic, DiagnosticSeverity, ParseResult, Position, Range

logger = logging.getLogger(__name__)

# OdooLS binary name (stable across distributions per research)
ODOOLS_BINARY = "odoo_ls_server"

# Default timeout for --parse mode (large workspaces can be slow)
DEFAULT_PARSE_TIMEOUT_S = 120


def _find_binary() -> str | None:
    """Return the path to odoo_ls_server, or None if not found."""
    return shutil.which(ODOOLS_BINARY)


def _build_command(
    workspace: Path,
    config_path: Path | None,
) -> list[str]:
    binary = _find_binary()
    if binary is None:
        raise FileNotFoundError(
            f"'{ODOOLS_BINARY}' not found on PATH. "
            "Install OdooLS and ensure it is on your PATH."
        )
    cmd = [binary, "--parse", str(workspace)]
    if config_path is not None:
        cmd += ["--config-path", str(config_path)]
    return cmd


def _parse_lsp_diagnostics(raw: dict[str, Any], workspace: str) -> list[Diagnostic]:
    """
    Convert LSP publishDiagnostics payload (or --parse JSON output) to Diagnostic list.

    OdooLS --parse emits a dict keyed by file URI with LSP diagnostic arrays.
    """
    results: list[Diagnostic] = []
    for uri, diags in raw.items():
        # Normalise URI → path
        file_path = uri
        if uri.startswith("file://"):
            file_path = uri[7:]

        if not isinstance(diags, list):
            continue

        for d in diags:
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


async def run_parse(
    workspace: str | Path,
    config_path: str | Path | None = None,
    timeout: float = DEFAULT_PARSE_TIMEOUT_S,
    min_severity: int = DiagnosticSeverity.HINT,
) -> ParseResult:
    """
    Run `odoo_ls_server --parse <workspace>` and return structured results.

    Args:
        workspace:     Absolute path to the Odoo workspace root.
        config_path:   Optional path to an odools.toml config file.
        timeout:       Seconds to wait before aborting (default 120).
        min_severity:  Only return diagnostics at or above this severity level.
                       1=error, 2=warning, 3=info, 4=hint (default: all).

    Returns:
        ParseResult with diagnostics list and metadata.
    """
    workspace = Path(workspace).resolve()
    config_path = Path(config_path).resolve() if config_path else None

    if not workspace.exists():
        raise ValueError(f"Workspace path does not exist: {workspace}")

    cmd = _build_command(workspace, config_path)
    logger.debug("Running: %s", " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # Prevent signal propagation to child
        start_new_session=True,
        # Ensure child does not inherit our stdin (avoids MCP stream corruption)
        stdin=asyncio.subprocess.DEVNULL,
    )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        raise TimeoutError(  # noqa: B904
            f"odoo_ls_server --parse timed out after {timeout}s. "
            "Consider increasing the timeout for large workspaces."
        )

    stderr_text = stderr_bytes.decode(errors="replace").strip()
    if stderr_text:
        # Log OdooLS stderr output for debugging but don't fail on it
        for line in stderr_text.splitlines():
            logger.debug("[odoo_ls_server stderr] %s", line)

    stdout_text = stdout_bytes.decode(errors="replace").strip()

    errors: list[str] = []
    raw: dict[str, Any] = {}
    diagnostics: list[Diagnostic] = []

    if proc.returncode != 0:
        errors.append(
            f"odoo_ls_server exited with code {proc.returncode}. "
            f"stderr: {stderr_text[:500]}"
        )
        logger.warning(
            "odoo_ls_server --parse exited %d: %s", proc.returncode, stderr_text[:200]
        )
    elif stdout_text:
        try:
            raw = json.loads(stdout_text)
            diagnostics = _parse_lsp_diagnostics(raw, str(workspace))
        except json.JSONDecodeError as exc:
            errors.append(f"Failed to parse JSON output from odoo_ls_server: {exc}")
            logger.error("JSON decode error: %s\nOutput was: %.500s", exc, stdout_text)

    # Apply severity filter
    if min_severity < DiagnosticSeverity.HINT:
        diagnostics = [d for d in diagnostics if d.severity <= min_severity]

    return ParseResult(
        workspace=str(workspace),
        files_analyzed=len(raw),
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
