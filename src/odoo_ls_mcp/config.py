"""
Configuration loading and workspace resolution for odoo-ls-mcp.

Resolution order for workspace_root / config_path:
1. Explicit arguments
2. Walk up from workspace_root (or CWD) searching for odools.toml
3. If no config found and no explicit override → raise ConfigError

Resolution order for odools_binary:
1. Explicit odools_binary arg
2. ODOO_LS_PATH env var
3. shutil.which("odoo_ls_server")
4. → raise ConfigError
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    pass


@dataclass
class WorkspaceConfig:
    workspace_root: Path
    config_path: Path | None
    odools_binary: Path
    idle_ttl_s: float = 300.0
    log_level: str = "WARNING"
    preview_len: int = 120


def _find_odools_toml(start: Path) -> Path | None:
    current = start.resolve()
    visited: set[Path] = set()
    while True:
        if current in visited:
            break
        visited.add(current)
        candidate = current / "odools.toml"
        if candidate.exists():
            return candidate.resolve()
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def _resolve_binary(odools_binary: str | None) -> Path:
    if odools_binary is not None:
        p = Path(odools_binary).resolve()
        if not p.exists():
            raise ConfigError(
                f"Explicit odools_binary path does not exist: {odools_binary!r}\n"
                "Verify the path is correct and the binary is executable."
            )
        return p

    env_path = os.environ.get("ODOO_LS_PATH")
    if env_path:
        p = Path(env_path).resolve()
        if not p.exists():
            raise ConfigError(
                f"ODOO_LS_PATH env var points to non-existent path: {env_path!r}\n"
                "Update ODOO_LS_PATH or unset it to fall back to PATH lookup."
            )
        return p

    found = shutil.which("odoo_ls_server")
    if found:
        return Path(found).resolve()

    raise ConfigError(
        "odoo_ls_server not found.\n"
        "Install OdooLS (https://github.com/odoo/odoo-ls) and ensure the binary\n"
        "directory is on PATH, or set the ODOO_LS_PATH environment variable."
    )


def resolve_config(
    workspace_root: str | Path | None = None,
    config_path: str | Path | None = None,
    odools_binary: str | None = None,
) -> WorkspaceConfig:
    """Resolve a WorkspaceConfig from arguments and environment.

    Args:
        workspace_root: Explicit workspace root. Defaults to CWD.
        config_path: Explicit path to odools.toml. If omitted, walks up from
            workspace_root to find one.
        odools_binary: Explicit path to odoo_ls_server binary.

    Returns:
        Fully resolved WorkspaceConfig with all paths canonicalized.

    Raises:
        ConfigError: If a required resource (config or binary) cannot be located.
    """
    resolved_root = (
        Path.cwd().resolve()
        if workspace_root is None
        else Path(workspace_root).resolve()
    )
    logger.debug("Resolved workspace_root: %s", resolved_root)

    resolved_config: Path | None
    if config_path is not None:
        resolved_config = Path(config_path).resolve()
        if not resolved_config.exists():
            raise ConfigError(
                f"Explicit config_path does not exist: {config_path!r}\n"
                "Verify the path points to a valid odools.toml file."
            )
        logger.debug("Using explicit config_path: %s", resolved_config)
    else:
        start_dir = resolved_root if resolved_root.is_dir() else resolved_root.parent
        resolved_config = _find_odools_toml(start_dir)
        if resolved_config is None:
            raise ConfigError(
                f"No odools.toml found walking up from: {resolved_root}\n\n"
                "OdooLS requires a config file. Create one at your workspace root:\n\n"
                "  [[config]]\n"
                '  odoo_path = "/path/to/odoo"\n'
                '  addons_paths = ["$autoDetectAddons"]\n\n'
                "Or pass config_path explicitly."
            )
        logger.debug("Auto-discovered config_path: %s", resolved_config)

    resolved_binary = _resolve_binary(odools_binary)
    logger.debug("Resolved odools_binary: %s", resolved_binary)

    return WorkspaceConfig(
        workspace_root=resolved_root,
        config_path=resolved_config,
        odools_binary=resolved_binary,
    )


def session_key(cfg: WorkspaceConfig) -> tuple[str, str | None]:
    """Return (str(workspace_root), str(config_path) or None) as a stable session key."""
    return (str(cfg.workspace_root), str(cfg.config_path) if cfg.config_path else None)
