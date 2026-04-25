"""
odoo-ls-mcp: FastMCP server entry point.

Exposes OdooLS capabilities as MCP tools callable by LLM agents.
Transport: stdio (default — never pollute stdout).

Run via:
    uv run odoo-ls-mcp
    python -m odoo_ls_mcp.server
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

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
        "Provides static analysis and diagnostics for Odoo addon workspaces. "
        "Requires `odoo_ls_server` to be installed and on PATH."
    ),
)


# ---------------------------------------------------------------------------
# Tool: parse_diagnostics
# ---------------------------------------------------------------------------


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
        Field(
            description=(
                "Absolute path to the Odoo workspace root directory. "
                "Must contain or be covered by an odools.toml config."
            )
        ),
    ],
    config_path: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Optional absolute path to an odools.toml config file. "
                "If omitted, OdooLS walks up from the workspace to find one."
            ),
        ),
    ] = None,
    timeout: Annotated[
        float,
        Field(
            default=DEFAULT_PARSE_TIMEOUT_S,
            description=(
                "Seconds to wait for analysis to complete. "
                "Large workspaces may need 120–300 s. Default: 120."
            ),
            ge=5,
            le=600,
        ),
    ] = DEFAULT_PARSE_TIMEOUT_S,
    min_severity: Annotated[
        int,
        Field(
            default=DiagnosticSeverity.WARNING,
            description=(
                "Minimum severity to include. "
                "1=error only, 2=warning+error, 3=info+, 4=all (hints). Default: 2."
            ),
            ge=1,
            le=4,
        ),
    ] = DiagnosticSeverity.WARNING,
) -> str:
    """Run OdooLS --parse and return a formatted diagnostics report."""
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


# ---------------------------------------------------------------------------
# Tool: check_odools_available
# ---------------------------------------------------------------------------


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
    import asyncio
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


# ---------------------------------------------------------------------------
# Tool: list_odools_config
# ---------------------------------------------------------------------------


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
            description=(
                "Directory to start searching from (typically your workspace root). "
                "The search walks up toward the filesystem root."
            )
        ),
    ],
) -> str:
    """Find odools.toml config files by walking up from start_path."""
    path = Path(start_path).resolve()
    if not path.exists():
        return f"❌ Path does not exist: {path}"

    found: list[Path] = []
    current = path if path.is_dir() else path.parent
    visited = set()

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
            "  odoo_path = \"/path/to/odoo\"\n"
            "  addons_paths = [\"$autoDetectAddons\"]"
        )

    lines = [f"📋 Found {len(found)} odools.toml file(s) searching from: {start_path}", ""]
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    mcp.run()  # defaults to stdio transport


if __name__ == "__main__":
    main()
