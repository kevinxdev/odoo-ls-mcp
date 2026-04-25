"""
odoo-ls-mcp: FastMCP server entry point.

Exposes OdooLS capabilities as MCP tools callable by LLM agents.
Transport: stdio (default — never pollute stdout).

Run via:
    uv run odoo-ls-mcp
    python -m odoo_ls_mcp.server
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

from .lsp_session import (
    INDEXING_READY_TIMEOUT,
    REQUEST_TIMEOUT,
    get_registry,
)
from .models import DiagnosticSeverity
from .parse_tool import (
    DEFAULT_PARSE_TIMEOUT_S,
    format_diagnostics_text,
    run_parse,
)

# All logging to stderr — stdout is reserved for MCP stdio transport
logging.basicConfig(
    stream=sys.stderr,
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="odoo-ls-mcp",
    instructions=(
        "MCP server wrapping OdooLS (Odoo Language Server). "
        "Provides static analysis, diagnostics, hover, go-to-definition, and "
        "completions for Odoo addon workspaces. "
        "Requires `odoo_ls_server` to be installed and on PATH. "
        "Call check_odools_available first to verify the environment."
    ),
)


# ── Helper ────────────────────────────────────────────────────────────────────


def _fmt_hover(result: dict | None) -> str:
    if result is None:
        return "No hover information available at this position."
    contents = result.get("contents", "")
    if isinstance(contents, str):
        return contents
    if isinstance(contents, dict):
        return contents.get("value", "No content")
    if isinstance(contents, list):
        parts = []
        for c in contents:
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, dict):
                parts.append(c.get("value", ""))
        return "\n\n".join(p for p in parts if p)
    return str(contents)


_SYMBOL_KIND = {
    1: "File",
    2: "Module",
    3: "Namespace",
    4: "Package",
    5: "Class",
    6: "Method",
    7: "Property",
    8: "Field",
    9: "Constructor",
    10: "Enum",
    11: "Interface",
    12: "Function",
    13: "Variable",
    14: "Constant",
    15: "String",
    16: "Number",
    17: "Boolean",
    18: "Array",
    19: "Object",
    20: "Key",
    21: "Null",
    22: "EnumMember",
    23: "Struct",
    24: "Event",
    25: "Operator",
    26: "TypeParameter",
}


def _uri_to_path(uri: str) -> str:
    return uri[7:] if uri.startswith("file://") else uri


def _fmt_locations(locations: list[dict], empty_msg: str = "No results found.") -> str:
    if not locations:
        return empty_msg
    lines = [f"Found {len(locations)} location(s):"]
    for loc in locations:
        uri = loc.get("uri", "")
        file_path = _uri_to_path(uri)
        r = loc.get("range", {})
        start = r.get("start", {})
        line = start.get("line", 0) + 1
        char = start.get("character", 0) + 1
        lines.append(f"  📍 {file_path}:{line}:{char}")
    return "\n".join(lines)


def _fmt_symbols(symbols: list[dict], source: str = "") -> str:
    if not symbols:
        return f"No symbols found{' for: ' + source if source else ''}."
    lines = [f"Found {len(symbols)} symbol(s){' for: ' + source if source else ''}:"]
    for sym in symbols[:200]:
        name = sym.get("name", "?")
        kind_val = sym.get("kind", 0)
        kind = _SYMBOL_KIND.get(kind_val, str(kind_val))
        container = sym.get("containerName", "")
        location = sym.get("location", {})
        if not location:
            loc_range = sym.get("range", sym.get("selectionRange", {}))
            uri = sym.get("uri", "")
            location = {"uri": uri, "range": loc_range}
        uri = location.get("uri", "")
        file_path = _uri_to_path(uri) if uri else ""
        r = location.get("range", {})
        start = r.get("start", {})
        ln = start.get("line", 0) + 1
        ch = start.get("character", 0) + 1
        row = f"  • [{kind}] {name}"
        if container:
            row += f"  (in {container})"
        if file_path:
            row += f"  — {file_path}:{ln}:{ch}"
        lines.append(row)
    if len(symbols) > 200:
        lines.append(f"  ... and {len(symbols) - 200} more")
    return "\n".join(lines)


def _fmt_completions(items: list[dict]) -> str:
    if not items:
        return "No completions available at this position."
    lines = [f"Found {len(items)} completion(s):"]
    for item in items[:50]:  # cap at 50
        label = item.get("label", "")
        kind = item.get("kind", 0)
        detail = item.get("detail", "")
        doc = item.get("documentation", "")
        if isinstance(doc, dict):
            doc = doc.get("value", "")
        kind_label = _COMPLETION_KIND.get(kind, "")
        row = f"  • {label}"
        if kind_label:
            row += f"  [{kind_label}]"
        if detail:
            row += f"  — {detail}"
        if doc:
            row += f"\n    {doc[:120]}"
        lines.append(row)
    if len(items) > 50:
        lines.append(f"  ... and {len(items) - 50} more")
    return "\n".join(lines)


_COMPLETION_KIND = {
    1: "Text",
    2: "Method",
    3: "Function",
    4: "Constructor",
    5: "Field",
    6: "Variable",
    7: "Class",
    8: "Interface",
    9: "Module",
    10: "Property",
    14: "Keyword",
    15: "Snippet",
    17: "Color",
    18: "File",
    19: "Reference",
    21: "Folder",
}


# ── Tool: check_odools_available ─────────────────────────────────────────────


@mcp.tool(
    name="check_odools_available",
    description=(
        "Check whether odoo_ls_server is installed and available on PATH. "
        "Returns version information if available. "
        "Call this first to verify the environment before running other tools."
    ),
)
async def check_odools_available() -> str:
    """Verify that odoo_ls_server is installed and reachable."""
    import shutil

    binary = shutil.which("odoo_ls_server")
    if binary is None:
        return (
            "❌ odoo_ls_server not found on PATH.\n\n"
            "Install OdooLS: https://github.com/odoo/odoo-ls\n"
            "Then ensure the binary directory is on your PATH."
        )
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        version_text = (stdout + stderr).decode(errors="replace").strip()
        return f"✅ odoo_ls_server found at: {binary}\n{version_text}"
    except TimeoutError:
        return f"⚠️ odoo_ls_server found at {binary} but --version timed out."
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ odoo_ls_server found at {binary} but failed to query version: {exc}"


# ── Tool: list_odools_config ──────────────────────────────────────────────────


@mcp.tool(
    name="list_odools_config",
    description=(
        "Search for odools.toml configuration files starting from a given directory. "
        "Walks up the directory tree (like OdooLS does) to find the active config. "
        "Useful for diagnosing why OdooLS may not analyse a workspace correctly."
    ),
)
async def list_odools_config(
    start_path: Annotated[
        str,
        Field(
            description="Directory to start searching from (typically your workspace root)."
        ),
    ],
) -> str:
    path = Path(start_path).resolve()
    if not path.exists():
        return f"❌ Path does not exist: {path}"

    found: list[Path] = []
    current = path if path.is_dir() else path.parent
    visited: set[Path] = set()
    while True:
        if current in visited:
            break
        visited.add(current)
        candidate = current / "odools.toml"
        if candidate.exists():
            found.append(candidate)
        parent = current.parent
        if parent == current:
            break
        current = parent

    if not found:
        return (
            f"⚠️ No odools.toml found searching upward from: {start_path}\n\n"
            "OdooLS will not analyse this workspace without a config file.\n"
            "Create an odools.toml in your workspace root with at minimum:\n\n"
            "  [[config]]\n"
            '  odoo_path = "/path/to/odoo"\n'
            '  addons_paths = ["$autoDetectAddons"]'
        )

    lines = [
        f"📋 Found {len(found)} odools.toml file(s) searching from: {start_path}",
        "",
    ]
    for i, p in enumerate(found):
        prefix = "✅ (nearest — active)" if i == 0 else f"  (ancestor #{i})"
        lines.append(f"{prefix}: {p}")
        try:
            content = p.read_text(encoding="utf-8")
            lines.append("  Content preview:")
            for line in content.splitlines()[:20]:
                lines.append(f"    {line}")
            if len(content.splitlines()) > 20:
                lines.append(f"    ... ({len(content.splitlines())} lines total)")
        except OSError as exc:
            lines.append(f"  ⚠️ Could not read: {exc}")
        lines.append("")
    return "\n".join(lines)


# ── Tool: parse_diagnostics ───────────────────────────────────────────────────


@mcp.tool(
    name="parse_diagnostics",
    description=(
        "Run a one-shot static analysis of an Odoo workspace using OdooLS "
        "(odoo_ls_server --parse). Returns diagnostics grouped by file. "
        "No long-lived process is kept alive. Use for CI checks or ad-hoc audits. "
        "Requires odoo_ls_server to be on PATH and odools.toml configured."
    ),
)
async def parse_diagnostics(
    workspace: Annotated[
        str,
        Field(description="Absolute path to the Odoo workspace root directory."),
    ],
    config_path: Annotated[
        str | None,
        Field(
            default=None,
            description="Optional absolute path to an odools.toml config file.",
        ),
    ] = None,
    timeout: Annotated[
        float,
        Field(
            default=DEFAULT_PARSE_TIMEOUT_S,
            ge=5,
            le=600,
            description="Seconds to wait. Large workspaces may need 120–300 s.",
        ),
    ] = DEFAULT_PARSE_TIMEOUT_S,
    min_severity: Annotated[
        int,
        Field(
            default=DiagnosticSeverity.WARNING,
            ge=1,
            le=4,
            description="1=error only, 2=warning+error, 3=info+, 4=all. Default: 2.",
        ),
    ] = DiagnosticSeverity.WARNING,
) -> str:
    try:
        result = await run_parse(
            workspace=workspace,
            config_path=config_path,
            timeout=timeout,
            min_severity=min_severity,
        )
    except FileNotFoundError as exc:
        return f"❌ OdooLS not found: {exc}"
    except ValueError as exc:
        return f"❌ Invalid argument: {exc}"
    except TimeoutError as exc:
        return f"⏱️ Timeout: {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error in parse_diagnostics")
        return f"❌ Unexpected error: {exc}"
    return format_diagnostics_text(result)


# ── Tool: start_session ───────────────────────────────────────────────────────


@mcp.tool(
    name="start_session",
    description=(
        "Start a live OdooLS session for a workspace. "
        "Spawns odoo_ls_server as a long-lived subprocess, performs the LSP "
        "initialize handshake, and waits for initial workspace indexing. "
        "Must be called before hover, go_to_definition, get_live_diagnostics, "
        "or completions. Idempotent — safe to call multiple times."
    ),
)
async def start_session(
    workspace: Annotated[
        str,
        Field(description="Absolute path to the Odoo workspace root."),
    ],
    config_path: Annotated[
        str | None,
        Field(default=None, description="Optional absolute path to odools.toml."),
    ] = None,
    wait_for_indexing: Annotated[
        bool,
        Field(default=True, description="Wait for initial indexing before returning."),
    ] = True,
    indexing_timeout: Annotated[
        float,
        Field(
            default=INDEXING_READY_TIMEOUT,
            ge=10,
            le=600,
            description="Max seconds to wait for indexing. Default: 120.",
        ),
    ] = INDEXING_READY_TIMEOUT,
) -> str:
    ws = Path(workspace).resolve()
    if not ws.exists():
        return f"❌ Workspace path does not exist: {ws}"

    cfg = Path(config_path).resolve() if config_path else None
    registry = get_registry()

    try:
        session = await registry.get_or_create(ws, cfg)
    except FileNotFoundError as exc:
        return f"❌ {exc}"
    except TimeoutError as exc:
        return f"⏱️ {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to start LSP session")
        return f"❌ Failed to start session: {exc}"

    if wait_for_indexing:
        indexed = await session.wait_for_indexing(timeout=indexing_timeout)
        status = (
            "✅ Ready (indexing complete)"
            if indexed
            else "⚠️ Ready (indexing still in progress)"
        )
    else:
        status = f"✅ Session started (state={session.state.name})"

    return f"{status}\n  Workspace: {ws}\n  State:     {session.state.name}"


# ── Tool: get_live_diagnostics ────────────────────────────────────────────────


@mcp.tool(
    name="get_live_diagnostics",
    description=(
        "Get diagnostics from a live OdooLS session. "
        "Returns the currently cached diagnostics pushed by the language server. "
        "Optionally opens a specific file to trigger diagnostics for it. "
        "Requires start_session to have been called first."
    ),
)
async def get_live_diagnostics(
    workspace: Annotated[
        str,
        Field(description="Absolute path to the Odoo workspace root."),
    ],
    file_path: Annotated[
        str | None,
        Field(default=None, description="Optional: absolute path to a specific file."),
    ] = None,
    min_severity: Annotated[
        int,
        Field(
            default=DiagnosticSeverity.WARNING,
            ge=1,
            le=4,
            description="1=error only, 2=warning+error, 3=info+, 4=all. Default: 2.",
        ),
    ] = DiagnosticSeverity.WARNING,
    wait_seconds: Annotated[
        float,
        Field(
            default=3.0,
            ge=0,
            le=30,
            description="Seconds to wait for fresh diagnostics after opening a file.",
        ),
    ] = 3.0,
) -> str:
    ws = Path(workspace).resolve()
    registry = get_registry()
    session = await registry.get(ws)

    if session is None or not session.is_ready:
        return "⚠️ No active session for this workspace.\nCall start_session first."

    # Optionally open a specific file to trigger diagnostics
    if file_path:
        fp = Path(file_path).resolve()
        if not fp.exists():
            return f"❌ File not found: {fp}"
        try:
            await session.open_document(fp)
            if wait_seconds > 0:
                try:
                    await session.wait_for_diagnostics(timeout=wait_seconds)
                except TimeoutError:
                    pass  # return whatever is cached
        except Exception as exc:  # noqa: BLE001
            return f"❌ Error opening file: {exc}"

    all_diags = session.get_all_diagnostics()

    # Filter to specific file if requested
    if file_path:
        fp_str = str(Path(file_path).resolve())
        all_diags = {k: v for k, v in all_diags.items() if k == fp_str}

    # Apply severity filter
    filtered: dict[str, list] = {}
    for path_str, diags in all_diags.items():
        kept = [d for d in diags if d.severity <= min_severity]
        if kept:
            filtered[path_str] = kept

    if not filtered:
        scope = f"file {file_path}" if file_path else f"workspace {ws}"
        return f"✅ No diagnostics (severity ≤ {min_severity}) in {scope}."

    severity_icons = {1: "🔴", 2: "🟡", 3: "🔵", 4: "⚪"}
    lines = [
        f"📋 Live diagnostics — {sum(len(v) for v in filtered.values())} issue(s) "
        f"across {len(filtered)} file(s)",
        "",
    ]
    for file_str, diags in sorted(filtered.items()):
        lines.append(f"📄 {file_str}  ({len(diags)} issue(s))")
        for d in sorted(diags, key=lambda x: x.range.start.line):
            icon = severity_icons.get(d.severity, "•")
            loc = f"L{d.range.start.line + 1}:{d.range.start.character + 1}"
            code = f"[{d.code}] " if d.code else ""
            lines.append(f"  {icon} {loc}  {code}{d.message}")
        lines.append("")

    return "\n".join(lines)


# ── Tool: hover ───────────────────────────────────────────────────────────────


@mcp.tool(
    name="hover",
    description=(
        "Get hover information (type, docs, signature) at a specific position "
        "in an Odoo source file using the live OdooLS session. "
        "Requires start_session to have been called first."
    ),
)
async def hover(
    workspace: Annotated[
        str, Field(description="Absolute path to the Odoo workspace root.")
    ],
    file_path: Annotated[str, Field(description="Absolute path to the source file.")],
    line: Annotated[int, Field(description="0-based line number.", ge=0)],
    character: Annotated[int, Field(description="0-based character offset.", ge=0)],
    timeout: Annotated[
        float, Field(default=REQUEST_TIMEOUT, ge=1, le=60)
    ] = REQUEST_TIMEOUT,
) -> str:
    ws = Path(workspace).resolve()
    fp = Path(file_path).resolve()
    registry = get_registry()
    session = await registry.get(ws)

    if session is None or not session.is_ready:
        return "⚠️ No active session. Call start_session first."
    if not fp.exists():
        return f"❌ File not found: {fp}"

    try:
        result = await session.hover(fp, line, character, timeout=timeout)
        return _fmt_hover(result)
    except TimeoutError:
        return f"⏱️ Hover request timed out after {timeout}s."
    except RuntimeError as exc:
        return f"❌ LSP error: {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.exception("hover tool error")
        return f"❌ Unexpected error: {exc}"


# ── Tool: go_to_definition ────────────────────────────────────────────────────


@mcp.tool(
    name="go_to_definition",
    description=(
        "Find the definition location of a symbol at a specific position "
        "in an Odoo source file using the live OdooLS session. "
        "Requires start_session to have been called first."
    ),
)
async def go_to_definition(
    workspace: Annotated[
        str, Field(description="Absolute path to the Odoo workspace root.")
    ],
    file_path: Annotated[str, Field(description="Absolute path to the source file.")],
    line: Annotated[int, Field(description="0-based line number.", ge=0)],
    character: Annotated[int, Field(description="0-based character offset.", ge=0)],
    timeout: Annotated[
        float, Field(default=REQUEST_TIMEOUT, ge=1, le=60)
    ] = REQUEST_TIMEOUT,
) -> str:
    ws = Path(workspace).resolve()
    fp = Path(file_path).resolve()
    registry = get_registry()
    session = await registry.get(ws)

    if session is None or not session.is_ready:
        return "⚠️ No active session. Call start_session first."
    if not fp.exists():
        return f"❌ File not found: {fp}"

    try:
        locations = await session.go_to_definition(fp, line, character, timeout=timeout)
        return _fmt_locations(
            locations, empty_msg="No definition found at this position."
        )
    except TimeoutError:
        return f"⏱️ Definition request timed out after {timeout}s."
    except RuntimeError as exc:
        return f"❌ LSP error: {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.exception("go_to_definition tool error")
        return f"❌ Unexpected error: {exc}"


# ── Tool: completions ─────────────────────────────────────────────────────────


@mcp.tool(
    name="completions",
    description=(
        "Get completion suggestions at a specific position in an Odoo source file. "
        "Useful for discovering available fields, methods, and model names. "
        "Requires start_session to have been called first."
    ),
)
async def completions(
    workspace: Annotated[
        str, Field(description="Absolute path to the Odoo workspace root.")
    ],
    file_path: Annotated[str, Field(description="Absolute path to the source file.")],
    line: Annotated[int, Field(description="0-based line number.", ge=0)],
    character: Annotated[int, Field(description="0-based character offset.", ge=0)],
    timeout: Annotated[
        float, Field(default=REQUEST_TIMEOUT, ge=1, le=60)
    ] = REQUEST_TIMEOUT,
) -> str:
    ws = Path(workspace).resolve()
    fp = Path(file_path).resolve()
    registry = get_registry()
    session = await registry.get(ws)

    if session is None or not session.is_ready:
        return "⚠️ No active session. Call start_session first."
    if not fp.exists():
        return f"❌ File not found: {fp}"

    try:
        items = await session.completions(fp, line, character, timeout=timeout)
        return _fmt_completions(items)
    except TimeoutError:
        return f"⏱️ Completion request timed out after {timeout}s."
    except RuntimeError as exc:
        return f"❌ LSP error: {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.exception("completions tool error")
        return f"❌ Unexpected error: {exc}"


# ── Tool: indexing_status ─────────────────────────────────────────────────────


@mcp.tool(
    name="indexing_status",
    description=(
        "Check the indexing/analysis status of all active OdooLS sessions. "
        "Shows whether each workspace is still indexing or fully ready. "
        "Also reports active progress tokens and their completion percentages."
    ),
)
async def indexing_status() -> str:
    registry = get_registry()
    status = registry.status()

    if not status:
        return "No active OdooLS sessions."

    lines = [f"📊 Active OdooLS sessions ({len(status)}):"]
    for ws_path, state_name in status.items():
        icon = {
            "READY": "✅",
            "INDEXING": "⏳",
            "STARTING": "🔄",
            "INITIALIZING": "🔄",
            "FAILED": "❌",
            "STOPPED": "⏹️",
            "SHUTTING_DOWN": "⏹️",
        }.get(state_name, "•")
        lines.append(f"  {icon} {ws_path}  [{state_name}]")

        # Show active progress tokens
        session = await registry.get(Path(ws_path))
        if session:
            active = [p for p in session._progress.values() if not p.done]
            for prog in active:
                pct = f" {prog.percentage}%" if prog.percentage is not None else ""
                lines.append(f"      ↳ {prog.title}: {prog.message}{pct}")

    return "\n".join(lines)


# ── Tool: restart_server ──────────────────────────────────────────────────────


@mcp.tool(
    name="restart_server",
    description=(
        "Restart the OdooLS session for a specific workspace. "
        "Use when the server has crashed, stale diagnostics are suspected, "
        "or after making significant changes to odools.toml. "
        "The old process is cleanly shut down before starting a new one."
    ),
)
async def restart_server(
    workspace: Annotated[
        str, Field(description="Absolute path to the Odoo workspace root.")
    ],
    config_path: Annotated[
        str | None,
        Field(
            default=None, description="Optional new config path to use after restart."
        ),
    ] = None,
    wait_for_indexing: Annotated[
        bool,
        Field(
            default=False,
            description="Wait for indexing after restart. Default: False.",
        ),
    ] = False,
) -> str:
    ws = Path(workspace).resolve()
    if not ws.exists():
        return f"❌ Workspace path does not exist: {ws}"

    cfg = Path(config_path).resolve() if config_path else None
    registry = get_registry()

    try:
        session = await registry.restart(ws, cfg)
    except FileNotFoundError as exc:
        return f"❌ {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.exception("restart_server error")
        return f"❌ Failed to restart: {exc}"

    if wait_for_indexing:
        indexed = await session.wait_for_indexing()
        suffix = "indexing complete" if indexed else "indexing in progress"
        return f"✅ Restarted and ready ({suffix}): {ws}"

    return f"✅ Restarted (state={session.state.name}): {ws}"


# ── Tool: stop_session ────────────────────────────────────────────────────────


@mcp.tool(
    name="stop_session",
    description=(
        "Stop the OdooLS session for a specific workspace, freeing the subprocess. "
        "The session can be restarted later with start_session."
    ),
)
async def stop_session(
    workspace: Annotated[
        str, Field(description="Absolute path to the Odoo workspace root.")
    ],
) -> str:
    ws = Path(workspace).resolve()
    registry = get_registry()
    session = await registry.get(ws)

    if session is None:
        return f"ℹ️ No active session for: {ws}"

    await session.stop()
    return f"✅ Session stopped: {ws}"


# ── Tool: find_references ─────────────────────────────────────────────────────


@mcp.tool(
    name="find_references",
    description=(
        "Find all references to a symbol at a specific position in an Odoo source "
        "file using the live OdooLS session. "
        "Requires start_session to have been called first."
    ),
)
async def find_references(
    workspace: Annotated[
        str, Field(description="Absolute path to the Odoo workspace root.")
    ],
    file_path: Annotated[str, Field(description="Absolute path to the source file.")],
    line: Annotated[int, Field(description="0-based line number.", ge=0)],
    character: Annotated[int, Field(description="0-based character offset.", ge=0)],
    include_declaration: Annotated[
        bool,
        Field(default=True, description="Include the declaration in results."),
    ] = True,
    timeout: Annotated[
        float, Field(default=REQUEST_TIMEOUT, ge=1, le=60)
    ] = REQUEST_TIMEOUT,
) -> str:
    ws = Path(workspace).resolve()
    fp = Path(file_path).resolve()
    registry = get_registry()
    session = await registry.get(ws)

    if session is None or not session.is_ready:
        return "⚠️ No active session. Call start_session first."
    if not fp.exists():
        return f"❌ File not found: {fp}"

    try:
        locations = await session.find_references(
            fp,
            line,
            character,
            include_declaration=include_declaration,
            timeout=timeout,
        )
        return _fmt_locations(
            locations, empty_msg="No references found at this position."
        )
    except TimeoutError:
        return f"⏱️ References request timed out after {timeout}s."
    except RuntimeError as exc:
        return f"❌ LSP error: {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.exception("find_references tool error")
        return f"❌ Unexpected error: {exc}"


# ── Tool: document_symbols ────────────────────────────────────────────────────


@mcp.tool(
    name="document_symbols",
    description=(
        "List all symbols (classes, methods, fields) in a specific Odoo source file "
        "using the live OdooLS session. "
        "Requires start_session to have been called first."
    ),
)
async def document_symbols(
    workspace: Annotated[
        str, Field(description="Absolute path to the Odoo workspace root.")
    ],
    file_path: Annotated[str, Field(description="Absolute path to the source file.")],
    timeout: Annotated[
        float, Field(default=REQUEST_TIMEOUT, ge=1, le=60)
    ] = REQUEST_TIMEOUT,
) -> str:
    ws = Path(workspace).resolve()
    fp = Path(file_path).resolve()
    registry = get_registry()
    session = await registry.get(ws)

    if session is None or not session.is_ready:
        return "⚠️ No active session. Call start_session first."
    if not fp.exists():
        return f"❌ File not found: {fp}"

    try:
        symbols = await session.document_symbols(fp, timeout=timeout)
        return _fmt_symbols(symbols, source=str(fp))
    except TimeoutError:
        return f"⏱️ Document symbols request timed out after {timeout}s."
    except RuntimeError as exc:
        return f"❌ LSP error: {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.exception("document_symbols tool error")
        return f"❌ Unexpected error: {exc}"


# ── Tool: workspace_symbols ───────────────────────────────────────────────────


@mcp.tool(
    name="workspace_symbols",
    description=(
        "Search for symbols matching a query string across the entire Odoo workspace "
        "using the live OdooLS session. "
        "Useful for finding model classes, field names, or XML-related identifiers. "
        "Requires start_session to have been called first."
    ),
)
async def workspace_symbols(
    workspace: Annotated[
        str, Field(description="Absolute path to the Odoo workspace root.")
    ],
    query: Annotated[
        str,
        Field(description="Symbol name or prefix to search for (empty string = all)."),
    ],
    timeout: Annotated[
        float, Field(default=REQUEST_TIMEOUT, ge=1, le=60)
    ] = REQUEST_TIMEOUT,
) -> str:
    ws = Path(workspace).resolve()
    registry = get_registry()
    session = await registry.get(ws)

    if session is None or not session.is_ready:
        return "⚠️ No active session. Call start_session first."

    try:
        symbols = await session.workspace_symbols(query, timeout=timeout)
        return _fmt_symbols(symbols, source=repr(query))
    except TimeoutError:
        return f"⏱️ Workspace symbols request timed out after {timeout}s."
    except RuntimeError as exc:
        return f"❌ LSP error: {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.exception("workspace_symbols tool error")
        return f"❌ Unexpected error: {exc}"


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    mcp.run()  # defaults to stdio transport


if __name__ == "__main__":
    main()
