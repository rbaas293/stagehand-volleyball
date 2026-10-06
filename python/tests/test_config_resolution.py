"""
Config path resolution for source checkouts and installed wheel/shiv binaries.

Covers Major M1: packaged installs must not look beside site-packages for
config.yaml. Resolution order:
  1. --config PATH
  2. $STAGEHAND_VOLLEYBALL_CONFIG
  3. ./config.yaml (cwd)
  4. repo-root / python/config.yaml only when _RUNNING_FROM_SOURCE
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "config.example.yaml"


@pytest.fixture()
def main_mod():
    import main

    return main


def _write_min_config(path: Path, captain: str = "Test Captain") -> Path:
    path.write_text(
        "\n".join(
            [
                f'captainName: "{captain}"',
                'teamName: ""',
                'day: "Sunday"',
                'league: "Test"',
                'leagueUrl: "https://example.league.ninja/leagues/division/1"',
                'mode: "lean"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_resolve_config_cli_flag_wins(main_mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _write_min_config(tmp_path / "from-cli.yaml", "From CLI")
    env = _write_min_config(tmp_path / "from-env.yaml", "From Env")
    _write_min_config(tmp_path / "config.yaml", "From Cwd")  # decoy; CLI must win
    monkeypatch.setenv(main_mod.CONFIG_ENV_VAR, str(env))
    monkeypatch.chdir(tmp_path)

    resolved = main_mod.resolve_config_path(cli)
    assert resolved.resolve() == cli.resolve()
    cfg = main_mod.load_config(cli)
    assert cfg.captain_name == ["From CLI"]


def test_resolve_config_env_var(main_mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _write_min_config(tmp_path / "env-config.yaml", "From Env")
    _write_min_config(tmp_path / "config.yaml", "From Cwd")
    monkeypatch.setenv(main_mod.CONFIG_ENV_VAR, str(env))
    monkeypatch.chdir(tmp_path)

    resolved = main_mod.resolve_config_path(None)
    assert resolved.resolve() == env.resolve()
    assert main_mod.load_config().captain_name == ["From Env"]


def test_resolve_config_cwd_yaml(main_mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cwd_cfg = _write_min_config(tmp_path / "config.yaml", "From Cwd")
    monkeypatch.delenv(main_mod.CONFIG_ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)

    resolved = main_mod.resolve_config_path(None)
    assert resolved.resolve() == cwd_cfg.resolve()
    assert main_mod.load_config().captain_name == ["From Cwd"]


def test_resolve_config_source_repo_root(
    main_mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When running from source with no cwd config, fall back to repo-root."""
    monkeypatch.delenv(main_mod.CONFIG_ENV_VAR, raising=False)
    # Empty cwd so ./config.yaml is missing; source fallback should still see
    # repo example only if we point PARENT_CONFIG at it — use a fake source tree.
    fake_root = tmp_path / "python"
    fake_root.mkdir()
    (fake_root / "requirements.txt").write_text("# marker\n", encoding="utf-8")
    repo_cfg = _write_min_config(tmp_path / "config.yaml", "From Repo")
    empty_cwd = tmp_path / "empty-cwd"
    empty_cwd.mkdir()

    monkeypatch.setattr(main_mod, "ROOT", fake_root)
    monkeypatch.setattr(main_mod, "_RUNNING_FROM_SOURCE", True)
    monkeypatch.setattr(main_mod, "PARENT_CONFIG", repo_cfg)
    monkeypatch.setattr(main_mod, "LOCAL_CONFIG", fake_root / "config.yaml")
    monkeypatch.chdir(empty_cwd)

    resolved = main_mod.resolve_config_path(None)
    assert resolved.resolve() == repo_cfg.resolve()


def test_resolve_config_packaged_skips_site_packages(
    main_mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installed wheel/shiv: do not use ROOT/config.yaml (site-packages)."""
    monkeypatch.delenv(main_mod.CONFIG_ENV_VAR, raising=False)
    site = tmp_path / "site-packages"
    site.mkdir()
    # A decoy config next to the "installed" module must be ignored.
    _write_min_config(site / "config.yaml", "From SitePackages")
    empty_cwd = tmp_path / "run-cwd"
    empty_cwd.mkdir()

    monkeypatch.setattr(main_mod, "ROOT", site)
    monkeypatch.setattr(main_mod, "_RUNNING_FROM_SOURCE", False)
    monkeypatch.setattr(main_mod, "PARENT_CONFIG", site.parent / "config.yaml")
    monkeypatch.setattr(main_mod, "LOCAL_CONFIG", site / "config.yaml")
    monkeypatch.chdir(empty_cwd)

    with pytest.raises(main_mod.ConfigError, match="Could not find config.yaml") as err:
        main_mod.resolve_config_path(None)
    msg = str(err.value)
    assert "cp config.example.yaml config.yaml" in msg
    assert str(empty_cwd / "config.yaml") in msg
    # Must not claim it found the site-packages decoy.
    assert "From SitePackages" not in msg


def test_resolve_config_missing_lists_tried_paths(
    main_mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(main_mod.CONFIG_ENV_VAR, raising=False)
    monkeypatch.setattr(main_mod, "_RUNNING_FROM_SOURCE", False)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(main_mod.ConfigError, match="Tried:") as err:
        main_mod.resolve_config_path(None)
    assert "cp config.example.yaml config.yaml" in str(err.value)


def test_check_config_cli(main_mod, capsys: pytest.CaptureFixture[str]) -> None:
    """--check-config loads example and exits without scraping."""
    main_mod.main(["--config", str(EXAMPLE_CONFIG), "--check-config"])
    out = capsys.readouterr().out
    assert "config:" in out
    assert "mode:" in out


def test_load_dotenv_includes_cwd(
    main_mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SMOKE_DOTENV_PROBE", raising=False)
    (tmp_path / ".env").write_text("SMOKE_DOTENV_PROBE=from-cwd\n", encoding="utf-8")
    # Pretend packaged so only cwd .env is consulted (plus uniqueness).
    monkeypatch.setattr(main_mod, "_RUNNING_FROM_SOURCE", False)
    main_mod.load_dotenv_files()
    assert os.environ.get("SMOKE_DOTENV_PROBE") == "from-cwd"
