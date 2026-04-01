from __future__ import annotations

import os
from pathlib import Path

from orche import backend
from orche.notify_hook import NOTIFY_DISCORD_SH


def test_ensure_managed_codex_home_rewrites_notify_config(tmp_path, monkeypatch):
    source_home = tmp_path / "source-codex"
    source_home.mkdir()
    (source_home / "hooks").mkdir()
    (source_home / "config.toml").write_text('model = "gpt-5"\nnotify = ["old"]\n', encoding="utf-8")
    monkeypatch.setattr(backend, "DEFAULT_CODEX_SOURCE_HOME", source_home)
    monkeypatch.setattr(backend, "DEFAULT_CODEX_HOME_ROOT", tmp_path / "managed")

    target = backend.ensure_managed_codex_home("repo-codex-main", discord_channel_id="1234567890")

    hook_path = Path(target) / "hooks" / "discord-turn-notify.sh"
    config_toml = (Path(target) / "config.toml").read_text(encoding="utf-8")

    assert hook_path.read_text(encoding="utf-8") == NOTIFY_DISCORD_SH
    assert '--session", "repo-codex-main"' in config_toml
    assert '--channel-id", "1234567890"' in config_toml


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
    monkeypatch.setattr(backend, "ensure_codex_running", lambda *args, **kwargs: "%1")

    managed_home = backend.ensure_managed_codex_home("demo-session", discord_channel_id="123")
    backend.save_meta(
        "demo-session",
        {
            "session": "demo-session",
            "cwd": str(tmp_path),
            "agent": "codex",
            "pane_id": "%1",
            "codex_home": str(managed_home),
            "codex_home_managed": True,
            "discord_channel_id": "123",
        },
    )

    pane_id = backend.ensure_session("demo-session", tmp_path, "codex")
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
    monkeypatch.setattr(backend, "ensure_codex_running", lambda *args, **kwargs: "%1")

    relative_cwd = Path(".")
    original_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        backend.ensure_session("absolute-cwd-session", relative_cwd, "codex", discord_channel_id="123")
    finally:
        os.chdir(original_cwd)

    meta = backend.load_meta("absolute-cwd-session")
    assert meta["cwd"] == str(tmp_path.resolve())


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
        backend.ensure_session("bound-session", second_cwd, "codex")
    except backend.OrcheError as exc:
        assert "already bound to cwd=" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected OrcheError for cwd mismatch")
