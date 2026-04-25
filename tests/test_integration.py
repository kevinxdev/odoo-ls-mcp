"""
Integration tests for parse_tool.run_parse against the real odoo_ls_server binary.

These tests are skipped automatically when odoo_ls_server is not on PATH.
Run them explicitly with:

    pytest -m integration tests/test_integration.py -v
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from odoo_ls_mcp.models import DiagnosticSeverity
from odoo_ls_mcp.parse_tool import run_parse

# ---------------------------------------------------------------------------
# Module-level skip if binary absent
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.integration

if not shutil.which("odoo_ls_server"):
    pytest.skip("odoo_ls_server not on PATH", allow_module_level=True)

# ---------------------------------------------------------------------------
# Paths for the real environment
# ---------------------------------------------------------------------------

ODOO_COMMUNITY = Path(
    "/home/kevin/Development/Odoo/athenrix-docker-base/odoo/custom/src/odoo"
)
STDLIB = Path.home() / ".local/share/odoo-ls/typeshed/stdlib"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_clean_addon(addon_dir: Path) -> None:
    """Write a minimal valid Odoo 19 addon with no intentional errors."""
    addon_dir.mkdir(parents=True, exist_ok=True)
    (addon_dir / "__manifest__.py").write_text(
        '{"name": "Test Addon", "version": "19.0.1.0.0", "depends": ["base"]}\n'
    )
    (addon_dir / "__init__.py").write_text("from . import models\n")
    models_dir = addon_dir / "models"
    models_dir.mkdir(exist_ok=True)
    (models_dir / "__init__.py").write_text("from . import test_model\n")
    (models_dir / "test_model.py").write_text(
        "from odoo import models\n\n\n"
        "class TestModel(models.Model):\n"
        "    _name = 'test.integration.model'\n"
    )


def _write_broken_addon(addon_dir: Path) -> None:
    """Write an addon that inherits a nonexistent model (triggers OLS05056)."""
    addon_dir.mkdir(parents=True, exist_ok=True)
    (addon_dir / "__manifest__.py").write_text(
        '{"name": "Broken Addon", "version": "19.0.1.0.0", "depends": ["base"]}\n'
    )
    (addon_dir / "__init__.py").write_text("from . import models\n")
    models_dir = addon_dir / "models"
    models_dir.mkdir(exist_ok=True)
    (models_dir / "__init__.py").write_text("from . import broken_model\n")
    # _inherit of a model that definitely doesn't exist → OLS05056
    (models_dir / "broken_model.py").write_text(
        "from odoo import models\n\n\n"
        "class BrokenModel(models.Model):\n"
        "    _inherit = 'this.nonexistent.model.zzz'\n"
    )


@pytest.fixture
def clean_addon(tmp_path: Path) -> tuple[Path, Path]:
    addon_dir = tmp_path / "clean_addon"
    _write_clean_addon(addon_dir)
    return tmp_path, addon_dir


@pytest.fixture
def broken_addon(tmp_path: Path) -> tuple[Path, Path]:
    addon_dir = tmp_path / "broken_addon"
    _write_broken_addon(addon_dir)
    return tmp_path, addon_dir


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _addon_diagnostics(result, addon_dir: Path):
    """Return only diagnostics whose file path is under addon_dir."""
    prefix = str(addon_dir)
    return [d for d in result.diagnostics if d.file.startswith(prefix)]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_binary_available():
    """Sanity check: the binary must be findable."""
    assert shutil.which("odoo_ls_server") is not None


@pytest.mark.asyncio
async def test_run_parse_clean_addon(clean_addon: tuple[Path, Path]):
    """A minimal valid addon should produce no errors inside the addon itself."""
    addon_parent, addon_dir = clean_addon
    result = await run_parse(
        addon_path=addon_parent,
        community_path=ODOO_COMMUNITY,
        tracked_path=addon_dir,
        stdlib_path=STDLIB if STDLIB.exists() else None,
        timeout=180,
    )
    assert result.errors == [], f"Unexpected run errors: {result.errors}"

    # Only care about diagnostics within our addon — community noise is expected
    addon_errors = [
        d
        for d in _addon_diagnostics(result, addon_dir)
        if d.severity == DiagnosticSeverity.ERROR
    ]
    assert addon_errors == [], f"Unexpected error diagnostics in addon: {addon_errors}"


@pytest.mark.asyncio
async def test_run_parse_detects_error(broken_addon: tuple[Path, Path]):
    """An addon with _inherit of a nonexistent model triggers OLS05056."""
    addon_parent, addon_dir = broken_addon
    result = await run_parse(
        addon_path=addon_parent,
        community_path=ODOO_COMMUNITY,
        tracked_path=addon_dir,
        stdlib_path=STDLIB if STDLIB.exists() else None,
        timeout=180,
    )
    assert result.errors == [], f"Unexpected run errors: {result.errors}"

    addon_diags = _addon_diagnostics(result, addon_dir)
    assert len(addon_diags) > 0, (
        "Expected at least one diagnostic inside the broken addon "
        f"(addon_dir={addon_dir}). All files seen: {sorted({d.file for d in result.diagnostics})}"
    )
    codes = {d.code for d in addon_diags}
    # OdooLS uses OLS03005 for unknown _inherit, OLS05056 for other model-not-found cases
    assert len(codes) > 0, (
        f"Expected at least one diagnostic code in addon, got: {codes}"
    )


@pytest.mark.asyncio
async def test_run_parse_nonexistent_addon_path():
    """Passing a non-existent addon_path must raise ValueError immediately."""
    with pytest.raises(ValueError, match="does not exist"):
        await run_parse(
            addon_path="/nonexistent/path/that/cannot/exist",
            community_path=ODOO_COMMUNITY,
        )


@pytest.mark.asyncio
async def test_run_parse_nonexistent_community_path(tmp_path: Path):
    """Passing a non-existent community_path must raise ValueError immediately."""
    with pytest.raises(ValueError, match="does not exist"):
        await run_parse(
            addon_path=tmp_path,
            community_path="/nonexistent/community/path",
        )


@pytest.mark.asyncio
async def test_run_parse_timeout(clean_addon: tuple[Path, Path]):
    """An extremely short timeout must raise TimeoutError."""
    addon_parent, addon_dir = clean_addon
    with pytest.raises(TimeoutError, match="timed out"):
        await run_parse(
            addon_path=addon_parent,
            community_path=ODOO_COMMUNITY,
            tracked_path=addon_dir,
            timeout=0.01,  # 10 ms — guaranteed to time out
        )
