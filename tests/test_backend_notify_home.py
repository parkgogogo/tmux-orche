from __future__ import annotations

import os
from pathlib import Path

import pytest

import backend
from notify_hook import NOTIFY_DISCORD_SH


def test_ensure_managed_codex_home_rewrites_notify_config(tmp_path, monkeypatch):
    source_home = tmp_path / "source-codex"
    source_home.mkdir()
    (source_home / "hooks").mkdir()
    (source_home / "config.toml").write_text('model = "gpt-5"\nnotify = ["old"]\n', encoding="utf-8")
    monkeypatch.setattr(backend, "DEFAULT_CODEX_SOURCE_HOME", source_home)
    monkeypatch.setattr(backend, "DEFAULT_CODEX_HOME_ROOT", tmp_path / "managed")

    target = backend.ensure_managed_codex_home(
        "repo-codex-main",
        cwd=tmp_path,
        discord_channel_id="1234567890",
    )

    hook_path = Path(target) / "hooks" / "discord-turn-notify.sh"
    config_toml = (Path(target) / "config.toml").read_text(encoding="utf-8")

    assert hook_path.read_text(encoding="utf-8") == NOTIFY_DISCORD_SH
    assert '--session", "repo-codex-main"' in config_toml
    assert '--channel-id", "1234567890"' in config_toml
    assert f'[projects."{tmp_path.resolve()}"]' in config_toml
    assert 'trust_level = "trusted"' in config_toml


def test_ensure_managed_codex_home_inserts_notify_before_tui_section(tmp_path, monkeypatch):
    source_home = tmp_path / "source-codex"
    source_home.mkdir()
    (source_home / "hooks").mkdir()
    (source_home / "config.toml").write_text(
        'model = "gpt-5"\n\n[tui]\nnotifications = ["agent-turn-complete"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(backend, "DEFAULT_CODEX_SOURCE_HOME", source_home)
    monkeypatch.setattr(backend, "DEFAULT_CODEX_HOME_ROOT", tmp_path / "managed")

    target = backend.ensure_managed_codex_home(
        "repo-codex-main",
        cwd=tmp_path,
        discord_channel_id="1234567890",
    )

    config_lines = (Path(target) / "config.toml").read_text(encoding="utf-8").splitlines()
    notify_index = next(index for index, line in enumerate(config_lines) if line.startswith("notify = "))
    tui_index = next(index for index, line in enumerate(config_lines) if line == "[tui]")

    assert notify_index < tui_index
    assert config_lines[tui_index + 1] == 'notifications = ["agent-turn-complete"]'


def test_ensure_managed_codex_home_prunes_runtime_artifacts(tmp_path, monkeypatch):
    source_home = tmp_path / "source-codex"
    source_home.mkdir()
    (source_home / "hooks").mkdir()
    (source_home / "log").mkdir()
    (source_home / "shell_snapshots").mkdir()
    (source_home / ".tmp").mkdir()
    (source_home / "tmp").mkdir()
    (source_home / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")
    (source_home / "history.jsonl").write_text("history\n", encoding="utf-8")
    (source_home / "logs_1.sqlite").write_text("sqlite\n", encoding="utf-8")
    (source_home / "logs_1.sqlite-wal").write_text("wal\n", encoding="utf-8")
    (source_home / "state_5.sqlite").write_text("state\n", encoding="utf-8")
    (source_home / "state_5.sqlite-wal").write_text("state-wal\n", encoding="utf-8")
    monkeypatch.setattr(backend, "DEFAULT_CODEX_SOURCE_HOME", source_home)
    monkeypatch.setattr(backend, "DEFAULT_CODEX_HOME_ROOT", tmp_path / "managed")

    target = Path(
        backend.ensure_managed_codex_home(
            "repo-codex-main",
            cwd=tmp_path,
            discord_channel_id="1234567890",
        )
    )

    assert not (target / "log").exists()
    assert not (target / "shell_snapshots").exists()
    assert not (target / ".tmp").exists()
    assert not (target / "tmp").exists()
    assert not (target / "history.jsonl").exists()
    assert not (target / "logs_1.sqlite").exists()
    assert not (target / "logs_1.sqlite-wal").exists()
    assert not (target / "state_5.sqlite").exists()
    assert not (target / "state_5.sqlite-wal").exists()
    assert (target / "hooks" / "discord-turn-notify.sh").exists()


def test_ensure_session_reuses_managed_codex_home_with_normalized_path(xdg_runtime, tmp_path, monkeypatch):
    source_home = tmp_path / "source-codex"
    source_home.mkdir()
    (source_home / "hooks").mkdir()
    (source_home / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")

    real_root = tmp_path / "real-root"
    real_root.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(real_root, target_is_directory=True)

    monkeypatch.setattr(backend, "DEFAULT_CODEX_SOURCE_HOME", source_home)
    monkeypatch.setattr(backend, "DEFAULT_CODEX_HOME_ROOT", linked_root)
    monkeypatch.setattr(backend, "ensure_pane", lambda session, cwd, agent: "%1")
    monkeypatch.setattr(backend, "ensure_agent_running", lambda *args, **kwargs: "%1")

    managed_home = backend.ensure_managed_codex_home("demo-session", cwd=tmp_path, discord_channel_id="123")
    backend.save_meta(
        "demo-session",
        {
            "session": "demo-session",
            "cwd": str(tmp_path),
            "agent": "codex",
            "pane_id": "%1",
            "codex_home": str(managed_home),
            "codex_home_managed": True,
            "notify_binding": {
                "provider": "discord",
                "target": "123",
                "session": "agent:main:discord:channel:123",
            },
        },
    )

    pane_id = backend.ensure_session(
        "demo-session",
        tmp_path,
        "codex",
        notify_to="discord",
        notify_target="123",
    )
    meta = backend.load_meta("demo-session")

    assert pane_id == "%1"
    assert meta["codex_home"] == str(managed_home)


def test_ensure_session_stores_absolute_cwd_in_metadata(xdg_runtime, tmp_path, monkeypatch):
    source_home = tmp_path / "source-codex"
    source_home.mkdir()
    (source_home / "hooks").mkdir()
    (source_home / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")

    monkeypatch.setattr(backend, "DEFAULT_CODEX_SOURCE_HOME", source_home)
    monkeypatch.setattr(backend, "DEFAULT_CODEX_HOME_ROOT", tmp_path / "managed")
    monkeypatch.setattr(backend, "ensure_pane", lambda session, cwd, agent: "%1")
    monkeypatch.setattr(backend, "ensure_agent_running", lambda *args, **kwargs: "%1")

    relative_cwd = Path(".")
    original_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        backend.ensure_session(
            "absolute-cwd-session",
            relative_cwd,
            "codex",
            notify_to="discord",
            notify_target="123",
        )
    finally:
        os.chdir(original_cwd)

    meta = backend.load_meta("absolute-cwd-session")
    assert meta["cwd"] == str(tmp_path.resolve())


def test_ensure_managed_codex_home_refreshes_config_from_source_each_time(tmp_path, monkeypatch):
    source_home = tmp_path / "source-codex"
    source_home.mkdir()
    (source_home / "hooks").mkdir()
    source_config = source_home / "config.toml"
    source_config.write_text('model = "gpt-5"\n', encoding="utf-8")
    monkeypatch.setattr(backend, "DEFAULT_CODEX_SOURCE_HOME", source_home)
    monkeypatch.setattr(backend, "DEFAULT_CODEX_HOME_ROOT", tmp_path / "managed")

    target = Path(backend.ensure_managed_codex_home("demo-session", cwd=tmp_path, discord_channel_id="123"))
    managed_config = target / "config.toml"
    managed_config.write_text('model = "stale"\nnotify = ["stale"]\n', encoding="utf-8")
    source_config.write_text('model = "gpt-5.4"\n', encoding="utf-8")

    backend.ensure_managed_codex_home("demo-session", cwd=tmp_path, discord_channel_id="123")

    refreshed = managed_config.read_text(encoding="utf-8")
    assert 'model = "gpt-5.4"' in refreshed
    assert 'model = "stale"' not in refreshed
    assert 'notify = ["stale"]' not in refreshed


def test_ensure_managed_codex_home_syncs_trust_to_source_with_backup(tmp_path, monkeypatch):
    source_home = tmp_path / "source-codex"
    source_home.mkdir()
    (source_home / "hooks").mkdir()
    source_config = source_home / "config.toml"
    source_config.write_text('model = "gpt-5"\n[notice]\nhide_full_access_warning = true\n', encoding="utf-8")
    monkeypatch.setattr(backend, "DEFAULT_CODEX_SOURCE_HOME", source_home)
    monkeypatch.setattr(backend, "DEFAULT_CODEX_HOME_ROOT", tmp_path / "managed")

    backend.ensure_managed_codex_home("demo-session", cwd=tmp_path, discord_channel_id="123")

    updated_source = source_config.read_text(encoding="utf-8")
    backup_path = source_config.with_name(source_config.name + backend.SOURCE_CONFIG_BACKUP_SUFFIX)

    assert 'model = "gpt-5"' in updated_source
    assert '[notice]' in updated_source
    assert f'[projects."{tmp_path.resolve()}"]' in updated_source
    assert 'notify = [' not in updated_source
    assert backup_path.exists()
    assert backup_path.read_text(encoding="utf-8") == 'model = "gpt-5"\n[notice]\nhide_full_access_warning = true\n'


def test_ensure_managed_codex_home_updates_existing_project_trust_without_overwriting_other_values(tmp_path, monkeypatch):
    source_home = tmp_path / "source-codex"
    source_home.mkdir()
    (source_home / "hooks").mkdir()
    source_config = source_home / "config.toml"
    source_config.write_text(
        'model = "gpt-5"\n'
        f'[projects."{tmp_path.resolve()}"]\n'
        'trust_level = "untrusted"\n'
        'extra = "keep"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(backend, "DEFAULT_CODEX_SOURCE_HOME", source_home)
    monkeypatch.setattr(backend, "DEFAULT_CODEX_HOME_ROOT", tmp_path / "managed")

    backend.ensure_managed_codex_home("demo-session", cwd=tmp_path, discord_channel_id="123")

    updated_source = source_config.read_text(encoding="utf-8")
    assert 'trust_level = "trusted"' in updated_source
    assert 'extra = "keep"' in updated_source


def test_ensure_managed_codex_home_refuses_to_modify_invalid_source_toml(tmp_path, monkeypatch):
    source_home = tmp_path / "source-codex"
    source_home.mkdir()
    (source_home / "hooks").mkdir()
    source_config = source_home / "config.toml"
    invalid = '[projects."/broken"]\ntrust_level = \n'
    source_config.write_text(invalid, encoding="utf-8")
    monkeypatch.setattr(backend, "DEFAULT_CODEX_SOURCE_HOME", source_home)
    monkeypatch.setattr(backend, "DEFAULT_CODEX_HOME_ROOT", tmp_path / "managed")

    try:
        backend.ensure_managed_codex_home("demo-session", cwd=tmp_path, discord_channel_id="123")
    except backend.OrcheError as exc:
        assert "invalid TOML" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected OrcheError for invalid source TOML")

    assert source_config.read_text(encoding="utf-8") == invalid
    backup_path = source_config.with_name(source_config.name + backend.SOURCE_CONFIG_BACKUP_SUFFIX)
    assert not backup_path.exists()


def test_ensure_session_rejects_rebinding_session_to_different_cwd(xdg_runtime, tmp_path, monkeypatch):
    first_cwd = tmp_path / "first"
    second_cwd = tmp_path / "second"
    first_cwd.mkdir()
    second_cwd.mkdir()
    backend.save_meta(
        "bound-session",
        {
            "session": "bound-session",
            "cwd": str(first_cwd.resolve()),
            "agent": "codex",
            "pane_id": "%1",
            "codex_home": "",
            "codex_home_managed": False,
        },
    )

    try:
        backend.ensure_session(
            "bound-session",
            second_cwd,
            "codex",
            notify_to="discord",
            notify_target="123",
        )
    except backend.OrcheError as exc:
        assert "already bound to cwd=" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected OrcheError for cwd mismatch")


def test_ensure_session_rejects_rebinding_notify_binding(xdg_runtime, tmp_path, monkeypatch):
    source_home = tmp_path / "source-codex"
    source_home.mkdir()
    (source_home / "hooks").mkdir()
    (source_home / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")

    monkeypatch.setattr(backend, "DEFAULT_CODEX_SOURCE_HOME", source_home)
    monkeypatch.setattr(backend, "DEFAULT_CODEX_HOME_ROOT", tmp_path / "managed")
    monkeypatch.setattr(backend, "ensure_pane", lambda session, cwd, agent: "%1")
    monkeypatch.setattr(backend, "ensure_agent_running", lambda *args, **kwargs: "%1")

    backend.ensure_session("notify-bound", tmp_path, "codex", notify_to="discord", notify_target="123")

    with pytest.raises(backend.OrcheError, match="already bound to notify_to=discord notify_target=123"):
        backend.ensure_session("notify-bound", tmp_path, "codex", notify_to="discord", notify_target="456")


def test_ensure_session_requires_notify_binding(xdg_runtime, tmp_path, monkeypatch):
    source_home = tmp_path / "source-codex"
    source_home.mkdir()
    (source_home / "hooks").mkdir()
    (source_home / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")

    monkeypatch.setattr(backend, "DEFAULT_CODEX_SOURCE_HOME", source_home)
    monkeypatch.setattr(backend, "DEFAULT_CODEX_HOME_ROOT", tmp_path / "managed")
    monkeypatch.setattr(backend, "ensure_pane", lambda session, cwd, agent: "%1")
    monkeypatch.setattr(backend, "ensure_agent_running", lambda *args, **kwargs: "%1")

    with pytest.raises(backend.OrcheError, match="session-new requires both --notify-to and --notify-target"):
        backend.ensure_session("notify-bound", tmp_path, "codex")


def test_ensure_session_stores_tmux_bridge_notify_binding(xdg_runtime, tmp_path, monkeypatch):
    source_home = tmp_path / "source-codex"
    source_home.mkdir()
    (source_home / "hooks").mkdir()
    (source_home / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")

    monkeypatch.setattr(backend, "DEFAULT_CODEX_SOURCE_HOME", source_home)
    monkeypatch.setattr(backend, "DEFAULT_CODEX_HOME_ROOT", tmp_path / "managed")
    monkeypatch.setattr(backend, "ensure_pane", lambda session, cwd, agent: "%1")
    monkeypatch.setattr(backend, "ensure_agent_running", lambda *args, **kwargs: "%1")

    backend.ensure_session(
        "notify-bound",
        tmp_path,
        "codex",
        notify_to="tmux-bridge",
        notify_target="target-session",
    )
    meta = backend.load_meta("notify-bound")

    assert meta["notify_binding"] == {
        "provider": "tmux-bridge",
        "target": "target-session",
    }


def test_close_session_removes_managed_codex_home(xdg_runtime, tmp_path, monkeypatch):
    codex_home = tmp_path / "orche-codex-close-test"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")
    backend.save_meta(
        "close-test",
        {
            "session": "close-test",
            "cwd": str(tmp_path),
            "agent": "codex",
            "pane_id": "",
            "codex_home": str(codex_home),
            "codex_home_managed": True,
        },
    )
    monkeypatch.setattr(backend, "bridge_resolve", lambda session: "")

    backend.close_session("close-test")

    assert not codex_home.exists()
    assert backend.load_meta("close-test") == {}
