"""
Config path resolution, env loading, and atomic output writes.
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


def test_missing_cli_config_does_not_fall_through_to_cwd(
    main_mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MJ1: --config pointing at a missing file must not silently use ./config.yaml."""
    _write_min_config(tmp_path / "config.yaml", "From Cwd")
    missing = tmp_path / "does-not-exist.yaml"
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(main_mod.CONFIG_ENV_VAR, raising=False)

    with pytest.raises(main_mod.ConfigError, match="--config not found") as err:
        main_mod.resolve_config_path(missing)
    assert str(missing) in str(err.value)


def test_missing_env_config_does_not_fall_through_to_cwd(
    main_mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MJ1: STAGEHAND_VOLLEYBALL_CONFIG missing file must not silently use cwd."""
    _write_min_config(tmp_path / "config.yaml", "From Cwd")
    missing = tmp_path / "env-missing.yaml"
    monkeypatch.setenv(main_mod.CONFIG_ENV_VAR, str(missing))
    monkeypatch.chdir(tmp_path)

    with pytest.raises(main_mod.ConfigError, match=main_mod.CONFIG_ENV_VAR) as err:
        main_mod.resolve_config_path(None)
    assert "not found" in str(err.value)
    assert str(missing) in str(err.value)


def test_check_config_cli(main_mod, capsys: pytest.CaptureFixture[str]) -> None:
    """--check-config loads example and exits without scraping."""
    main_mod.main(["--config", str(EXAMPLE_CONFIG), "--check-config"])
    out = capsys.readouterr().out
    assert "config:" in out
    assert "mode:" in out


def test_dotenv_does_not_autoload_cwd(
    main_mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    (tmp_path / ".env").write_text("XAI_API_KEY=from-cwd\n", encoding="utf-8")
    monkeypatch.setattr(main_mod, "_RUNNING_FROM_SOURCE", False)
    main_mod.load_dotenv_files()
    assert os.environ.get("XAI_API_KEY") is None


def test_dotenv_env_file_flag_allowlist(
    main_mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = tmp_path / "secrets.env"
    env_path.write_text(
        "XAI_API_KEY=from-file\nUNKNOWN_SECRET=nope\nSCRAPE_MODE=llm\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("UNKNOWN_SECRET", raising=False)
    monkeypatch.delenv("SCRAPE_MODE", raising=False)
    monkeypatch.setattr(main_mod, "_RUNNING_FROM_SOURCE", False)
    main_mod.load_dotenv_files(env_path)
    assert os.environ.get("XAI_API_KEY") == "from-file"
    assert os.environ.get("SCRAPE_MODE") == "llm"
    assert os.environ.get("UNKNOWN_SECRET") is None


def test_dotenv_missing_env_file_raises(
    main_mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_mod, "_RUNNING_FROM_SOURCE", False)
    with pytest.raises(main_mod.ConfigError, match="--env-file not found"):
        main_mod.load_dotenv_files(tmp_path / "missing.env")


def test_write_output_atomic_replaces_file(main_mod, tmp_path: Path) -> None:
    target = tmp_path / "games.json"
    target.write_text("old\n", encoding="utf-8")
    main_mod.write_output_atomic(target, '{"ok": true}\n')
    assert target.read_text(encoding="utf-8") == '{"ok": true}\n'
    assert not target.is_symlink()


def test_write_output_atomic_refuses_symlink(main_mod, tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text("secret\n", encoding="utf-8")
    link = tmp_path / "games.json"
    link.symlink_to(real)
    with pytest.raises(main_mod.ConfigError, match="symlink"):
        main_mod.write_output_atomic(link, '{"nope": true}\n')
    assert real.read_text(encoding="utf-8") == "secret\n"


def test_make_grok_generate_uses_trust_env_false_by_default(main_mod, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(main_mod.httpx, "AsyncClient", FakeAsyncClient)

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured["openai_kwargs"] = kwargs

    monkeypatch.setattr(main_mod, "AsyncOpenAI", FakeOpenAI)
    main_mod.make_grok_generate("key", "grok-test")
    assert captured.get("trust_env") is False
