from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from odoo_ls_mcp.config import ConfigError, WorkspaceConfig, resolve_config, session_key

REAL_ODOO_WORKSPACE = Path("/home/kevin/Development/Odoo/athenrix-docker-base")
REAL_ODOOLS_TOML = REAL_ODOO_WORKSPACE / "odools.toml"


@pytest.fixture()
def fake_binary(tmp_path: Path) -> Path:
    binary = tmp_path / "odoo_ls_server"
    binary.write_text("#!/bin/sh\necho fake")
    binary.chmod(0o755)
    return binary


class TestExplicitArgs:
    def test_explicit_workspace_and_config(
        self, tmp_path: Path, fake_binary: Path
    ) -> None:
        config_file = tmp_path / "odools.toml"
        config_file.write_text("[[config]]\nodoo_path = './odoo'\n")

        cfg = resolve_config(
            workspace_root=tmp_path,
            config_path=config_file,
            odools_binary=str(fake_binary),
        )

        assert cfg.workspace_root == tmp_path.resolve()
        assert cfg.config_path == config_file.resolve()
        assert cfg.odools_binary == fake_binary.resolve()

    def test_explicit_args_resolve_symlinks(
        self, tmp_path: Path, fake_binary: Path
    ) -> None:
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        link_dir = tmp_path / "link"
        link_dir.symlink_to(real_dir)

        config_file = real_dir / "odools.toml"
        config_file.write_text("[[config]]\nodoo_path = './odoo'\n")

        cfg = resolve_config(
            workspace_root=link_dir,
            config_path=config_file,
            odools_binary=str(fake_binary),
        )

        assert cfg.workspace_root == real_dir.resolve()
        assert cfg.config_path == config_file.resolve()

    def test_explicit_config_not_found_raises(
        self, tmp_path: Path, fake_binary: Path
    ) -> None:
        with pytest.raises(ConfigError, match="Explicit config_path does not exist"):
            resolve_config(
                workspace_root=tmp_path,
                config_path=tmp_path / "nonexistent.toml",
                odools_binary=str(fake_binary),
            )

    def test_explicit_binary_not_found_raises(self, tmp_path: Path) -> None:
        config_file = tmp_path / "odools.toml"
        config_file.write_text("")
        with pytest.raises(
            ConfigError, match="Explicit odools_binary path does not exist"
        ):
            resolve_config(
                workspace_root=tmp_path,
                config_path=config_file,
                odools_binary="/nonexistent/odoo_ls_server",
            )


class TestWalkUpDiscovery:
    @pytest.mark.skipif(
        not REAL_ODOOLS_TOML.exists(),
        reason="Real odools.toml not present",
    )
    def test_walk_finds_real_config(self, fake_binary: Path) -> None:
        subdir = REAL_ODOO_WORKSPACE / "odoo" / "custom" / "src"
        cfg = resolve_config(
            workspace_root=subdir,
            odools_binary=str(fake_binary),
        )
        assert cfg.config_path == REAL_ODOOLS_TOML.resolve()
        assert cfg.workspace_root == subdir.resolve()

    def test_walk_finds_config_in_parent(
        self, tmp_path: Path, fake_binary: Path
    ) -> None:
        config_file = tmp_path / "odools.toml"
        config_file.write_text("[[config]]\nodoo_path = './odoo'\n")
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)

        cfg = resolve_config(
            workspace_root=deep,
            odools_binary=str(fake_binary),
        )

        assert cfg.config_path == config_file.resolve()

    def test_missing_config_raises_config_error(
        self, tmp_path: Path, fake_binary: Path
    ) -> None:
        with pytest.raises(ConfigError, match="No odools.toml found walking up from"):
            resolve_config(
                workspace_root=tmp_path,
                odools_binary=str(fake_binary),
            )

    def test_missing_config_error_includes_helpful_message(
        self, tmp_path: Path, fake_binary: Path
    ) -> None:
        with pytest.raises(ConfigError) as exc_info:
            resolve_config(workspace_root=tmp_path, odools_binary=str(fake_binary))
        msg = str(exc_info.value)
        assert "odools.toml" in msg
        assert "[[config]]" in msg


class TestBinaryResolution:
    def test_env_var_takes_precedence_over_which(self, tmp_path: Path) -> None:
        config_file = tmp_path / "odools.toml"
        config_file.write_text("")
        env_binary = tmp_path / "env_odoo_ls_server"
        env_binary.write_text("#!/bin/sh")
        env_binary.chmod(0o755)

        with patch.dict(os.environ, {"ODOO_LS_PATH": str(env_binary)}):
            cfg = resolve_config(workspace_root=tmp_path, config_path=config_file)

        assert cfg.odools_binary == env_binary.resolve()

    def test_which_fallback(self, tmp_path: Path, fake_binary: Path) -> None:
        config_file = tmp_path / "odools.toml"
        config_file.write_text("")

        env = {k: v for k, v in os.environ.items() if k != "ODOO_LS_PATH"}
        with patch.dict(os.environ, env, clear=True):
            with patch("shutil.which", return_value=str(fake_binary)):
                cfg = resolve_config(workspace_root=tmp_path, config_path=config_file)

        assert cfg.odools_binary == fake_binary.resolve()

    def test_binary_not_found_raises(self, tmp_path: Path) -> None:
        config_file = tmp_path / "odools.toml"
        config_file.write_text("")

        env = {k: v for k, v in os.environ.items() if k != "ODOO_LS_PATH"}
        with patch.dict(os.environ, env, clear=True):
            with patch("shutil.which", return_value=None):
                with pytest.raises(ConfigError, match="odoo_ls_server not found"):
                    resolve_config(workspace_root=tmp_path, config_path=config_file)

    def test_env_var_nonexistent_path_raises(self, tmp_path: Path) -> None:
        config_file = tmp_path / "odools.toml"
        config_file.write_text("")

        with patch.dict(os.environ, {"ODOO_LS_PATH": "/nonexistent/odoo_ls_server"}):
            with pytest.raises(ConfigError, match="ODOO_LS_PATH env var"):
                resolve_config(workspace_root=tmp_path, config_path=config_file)


class TestDefaults:
    def test_default_values(self, tmp_path: Path, fake_binary: Path) -> None:
        config_file = tmp_path / "odools.toml"
        config_file.write_text("")
        cfg = resolve_config(
            workspace_root=tmp_path,
            config_path=config_file,
            odools_binary=str(fake_binary),
        )
        assert cfg.idle_ttl_s == 300.0
        assert cfg.log_level == "WARNING"
        assert cfg.preview_len == 120


class TestSessionKey:
    def test_returns_stable_tuple(self, tmp_path: Path, fake_binary: Path) -> None:
        config_file = tmp_path / "odools.toml"
        config_file.write_text("")
        cfg = resolve_config(
            workspace_root=tmp_path,
            config_path=config_file,
            odools_binary=str(fake_binary),
        )
        key = session_key(cfg)
        assert isinstance(key, tuple)
        assert len(key) == 2
        assert key == session_key(cfg)

    def test_key_contains_string_paths(self, tmp_path: Path, fake_binary: Path) -> None:
        config_file = tmp_path / "odools.toml"
        config_file.write_text("")
        cfg = resolve_config(
            workspace_root=tmp_path,
            config_path=config_file,
            odools_binary=str(fake_binary),
        )
        key = session_key(cfg)
        assert isinstance(key[0], str)
        assert isinstance(key[1], str)

    def test_none_config_path_gives_none_in_key(
        self, tmp_path: Path, fake_binary: Path
    ) -> None:
        cfg = WorkspaceConfig(
            workspace_root=tmp_path,
            config_path=None,
            odools_binary=fake_binary,
        )
        key = session_key(cfg)
        assert key[1] is None

    def test_different_workspaces_give_different_keys(
        self, tmp_path: Path, fake_binary: Path
    ) -> None:
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        cfg_a = WorkspaceConfig(
            workspace_root=dir_a, config_path=None, odools_binary=fake_binary
        )
        cfg_b = WorkspaceConfig(
            workspace_root=dir_b, config_path=None, odools_binary=fake_binary
        )
        assert session_key(cfg_a) != session_key(cfg_b)
