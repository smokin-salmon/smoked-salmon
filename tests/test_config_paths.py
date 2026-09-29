"""Where salmon looks for config.toml, including the container single-mount case."""

import salmon.config as config


def test_unset_config_dir_env_uses_platform_dir(monkeypatch) -> None:
    monkeypatch.delenv(config.CONFIG_DIR_ENV, raising=False)
    path = config.get_user_cfg_path()
    assert path.name == "config.toml"
    assert path == config._platform_cfg_path()


def test_config_dir_env_overrides_platform_dir(monkeypatch, tmp_path) -> None:
    # A container mounts a single directory at $SALMON_CONFIG_DIR; the file must sit
    # directly in it rather than under a platformdirs app-name subdirectory. Give it a
    # config.toml so the legacy-fallback path (tested separately) does not kick in.
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv(config.CONFIG_DIR_ENV, str(tmp_path))
    path = config.get_user_cfg_path()
    assert path == tmp_path / "config.toml"


def test_config_dir_env_expands_user(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    (home / "salmon-cfg").mkdir(parents=True)
    (home / "salmon-cfg" / "config.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Path.expanduser() uses this on Windows
    monkeypatch.setenv(config.CONFIG_DIR_ENV, "~/salmon-cfg")
    path = config.get_user_cfg_path()
    assert path == home / "salmon-cfg" / "config.toml"


def test_config_dir_env_falls_back_to_legacy_path_when_missing(monkeypatch, tmp_path, capsys) -> None:
    # Upgrading the image without moving config.toml into the new mount: keep working
    # off the old platform-config-dir path rather than telling the user config is missing.
    legacy_path = tmp_path / "legacy" / "config.toml"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "_platform_cfg_path", lambda: legacy_path)
    monkeypatch.setattr(config, "_warned_legacy_config_fallback", False)

    new_dir = tmp_path / "config"
    monkeypatch.setenv(config.CONFIG_DIR_ENV, str(new_dir))

    path = config.get_user_cfg_path()
    assert path == legacy_path
    # Asserting on the path alone would also pass if the fallback did not run at all
    # (e.g. get_user_cfg_path() always returning _platform_cfg_path()); the warning
    # on stderr is only printed on the fallback branch, so it proves the branch ran.
    assert f"{config.CONFIG_DIR_ENV} is set to" in capsys.readouterr().err


def test_config_dir_env_used_once_legacy_fallback_file_exists(monkeypatch, tmp_path) -> None:
    legacy_path = tmp_path / "legacy" / "config.toml"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "_platform_cfg_path", lambda: legacy_path)

    new_dir = tmp_path / "config"
    new_dir.mkdir()
    (new_dir / "config.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv(config.CONFIG_DIR_ENV, str(new_dir))

    path = config.get_user_cfg_path()
    assert path == new_dir / "config.toml"
