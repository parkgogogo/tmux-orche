from __future__ import annotations

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
