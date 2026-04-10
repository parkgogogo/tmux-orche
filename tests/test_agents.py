from __future__ import annotations

import json
import os
import sys
import subprocess
import threading
import time
from pathlib import Path

import backend
import pytest
from agents.claude import CLAUDE_SUBMIT_SETTLE_SECONDS, ClaudeAgent
from agents.codex import (
    CODEX_SUBMIT_SETTLE_MAX_SECONDS,
    CODEX_SUBMIT_SETTLE_MIN_SECONDS,
    SOURCE_CONFIG_LOCK_NAME,
    CodexAgent,
    codex_submit_settle_seconds,
    source_config_lock,
)
from paths import locks_dir


class FakeBridge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    def type(self, session: str, text: str) -> None:
        self.calls.append(("type", session, text))

    def keys(self, session: str, keys: list[str]) -> None:
        self.calls.append(("keys", session, list(keys)))


def test_supported_agents_include_codex_and_claude():
    assert backend.supported_agent_names() == ("claude", "codex")


def test_ensure_managed_claude_home_writes_runtime_hooks(xdg_runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(backend.claude_agent_module, "DEFAULT_RUNTIME_HOME_ROOT", tmp_path / "managed")
    monkeypatch.setattr(backend.claude_agent_module, "source_claude_config_path", lambda: tmp_path / ".claude.json")
    monkeypatch.setattr(backend.claude_agent_module, "source_claude_home_path", lambda: tmp_path / ".claude")
    monkeypatch.setattr(
        backend.claude_agent_module,
        "source_claude_config_backup_path",
        lambda: tmp_path / ".claude.json.orche.bak",
    )
    source_home = tmp_path / ".claude"
    source_home.mkdir()

    target = backend.ensure_managed_claude_home(
        "repo-claude-main",
        cwd=tmp_path,
        discord_channel_id="1234567890",
    )

    settings_path = Path(target) / "settings.json"
    hook_path = Path(target) / "hooks" / "discord-turn-notify.sh"
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    command = payload["hooks"]["Stop"][0]["hooks"][0]["command"]
    source_payload = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))

    assert hook_path.exists()
    assert "--session repo-claude-main" in command
    assert "--channel-id 1234567890" in command
    assert payload["hooks"]["SessionStart"][0]["matcher"] == "startup"
    assert payload["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"].endswith("--channel-id 1234567890")
    assert payload["hooks"]["Notification"][0]["hooks"][0]["command"].endswith("--channel-id 1234567890 --status warning")
    assert payload["hooks"]["PermissionRequest"][0]["hooks"][0]["command"].endswith("--channel-id 1234567890 --status warning")
    assert source_payload["projects"][str(tmp_path.resolve())]["hasTrustDialogAccepted"] is True
    assert not (Path(target) / ".claude.json").exists()
    assert not (Path(target) / ".claude").exists()


def test_ensure_managed_claude_home_copies_source_config_into_runtime_settings(xdg_runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(backend.claude_agent_module, "DEFAULT_RUNTIME_HOME_ROOT", tmp_path / "managed")
    monkeypatch.setattr(backend.claude_agent_module, "source_claude_config_path", lambda: tmp_path / ".claude.json")
    monkeypatch.setattr(backend.claude_agent_module, "source_claude_home_path", lambda: tmp_path / ".claude")
    monkeypatch.setattr(
        backend.claude_agent_module,
        "source_claude_config_backup_path",
        lambda: tmp_path / ".claude.json.orche.bak",
    )
    source_home = tmp_path / ".claude"
    source_home.mkdir()
    (source_home / "settings.json").write_text(
        json.dumps(
            {
                "numStartups": 12,
                "theme": "dark-dimmed",
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "startup",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "/bin/echo existing-session-start-hook",
                                }
                            ],
                        }
                    ],
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "/bin/echo existing-stop-hook",
                                }
                            ]
                        }
                    ]
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    source_config_path = tmp_path / ".claude.json"
    source_config_path.write_text(
        json.dumps(
            {
                "projects": {
                    str(tmp_path.resolve()): {
                        "allowedTools": ["Bash(git status:*)"],
                    }
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    target = backend.ensure_managed_claude_home(
        "repo-claude-main",
        cwd=tmp_path,
        discord_channel_id=None,
    )

    settings_payload = json.loads((Path(target) / "settings.json").read_text(encoding="utf-8"))

    assert settings_payload["numStartups"] == 12
    assert settings_payload["theme"] == "dark-dimmed"
    assert settings_payload["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "/bin/echo existing-session-start-hook"
    assert "--session repo-claude-main" in settings_payload["hooks"]["SessionStart"][1]["hooks"][0]["command"]
    assert settings_payload["hooks"]["Stop"][0]["hooks"][0]["command"] == "/bin/echo existing-stop-hook"
    assert "--session repo-claude-main" in settings_payload["hooks"]["Stop"][1]["hooks"][0]["command"]
    assert not (Path(target) / ".claude.json").exists()
    assert not (Path(target) / ".claude").exists()


def test_ensure_managed_codex_home_disables_update_checks_without_mutating_source_setting(xdg_runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "DEFAULT_CODEX_HOME_ROOT", tmp_path / "managed")
    monkeypatch.setattr(backend, "DEFAULT_CODEX_SOURCE_HOME", tmp_path / ".codex")
    monkeypatch.setattr(backend.codex_agent_module, "DEFAULT_RUNTIME_HOME_ROOT", tmp_path / "managed")
    monkeypatch.setattr(backend.codex_agent_module, "DEFAULT_CODEX_SOURCE_HOME", tmp_path / ".codex")

    source_home = tmp_path / ".codex"
    source_home.mkdir()
    source_config_path = source_home / "config.toml"
    source_config_path.write_text(
        'model = "gpt-5"\ncheck_for_update_on_startup = true\n\n[notice]\nhide_rate_limit_model_nudge = false\n',
        encoding="utf-8",
    )

    target = backend.ensure_managed_codex_home(
        "repo-codex-main",
        cwd=tmp_path,
        discord_channel_id="1234567890",
    )

    managed_config = (Path(target) / "config.toml").read_text(encoding="utf-8")
    hooks_payload = json.loads((Path(target) / "hooks.json").read_text(encoding="utf-8"))
    source_config = source_config_path.read_text(encoding="utf-8")

    assert 'check_for_update_on_startup = false' in managed_config
    assert 'check_for_update_on_startup = true' in source_config
    assert 'hide_rate_limit_model_nudge = true' in managed_config
    assert 'hide_rate_limit_model_nudge = false' in source_config
    assert 'notify = ["/bin/bash"' in managed_config
    assert "codex_hooks = true" in managed_config
    assert f'[projects.{json.dumps(str(tmp_path.resolve()))}]' in managed_config
    assert 'trust_level = "trusted"' in managed_config
    assert hooks_payload["hooks"]["SessionStart"][0]["matcher"] == "startup"
    assert "--session repo-codex-main" in hooks_payload["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert "--channel-id 1234567890" in hooks_payload["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert hooks_payload["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"].endswith(">/dev/null")
    assert "Stop" not in hooks_payload["hooks"]


def test_ensure_managed_codex_home_preserves_existing_hooks_json(xdg_runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "DEFAULT_CODEX_HOME_ROOT", tmp_path / "managed")
    monkeypatch.setattr(backend, "DEFAULT_CODEX_SOURCE_HOME", tmp_path / ".codex")
    monkeypatch.setattr(backend.codex_agent_module, "DEFAULT_RUNTIME_HOME_ROOT", tmp_path / "managed")
    monkeypatch.setattr(backend.codex_agent_module, "DEFAULT_CODEX_SOURCE_HOME", tmp_path / ".codex")

    source_home = tmp_path / ".codex"
    source_home.mkdir()
    (source_home / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")
    (source_home / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "startup",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "/bin/echo existing-session-start-hook",
                                }
                            ],
                        }
                    ],
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "/bin/echo existing-stop-hook",
                                }
                            ]
                        }
                    ],
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    target = backend.ensure_managed_codex_home(
        "repo-codex-main",
        cwd=tmp_path,
        discord_channel_id=None,
    )

    hooks_payload = json.loads((Path(target) / "hooks.json").read_text(encoding="utf-8"))

    assert hooks_payload["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "/bin/echo existing-session-start-hook"
    assert "--session repo-codex-main" in hooks_payload["hooks"]["SessionStart"][1]["hooks"][0]["command"]
    assert hooks_payload["hooks"]["Stop"][0]["hooks"][0]["command"] == "/bin/echo existing-stop-hook"


def test_ensure_managed_codex_home_preserves_state_files_needed_for_login(xdg_runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "DEFAULT_CODEX_HOME_ROOT", tmp_path / "managed")
    monkeypatch.setattr(backend, "DEFAULT_CODEX_SOURCE_HOME", tmp_path / ".codex")
    monkeypatch.setattr(backend.codex_agent_module, "DEFAULT_RUNTIME_HOME_ROOT", tmp_path / "managed")
    monkeypatch.setattr(backend.codex_agent_module, "DEFAULT_CODEX_SOURCE_HOME", tmp_path / ".codex")

    source_home = tmp_path / ".codex"
    source_home.mkdir()
    (source_home / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")
    (source_home / "auth.json").write_text('{"token":"auth"}\n', encoding="utf-8")
    (source_home / "mcp.json").write_text('{"mcpServers":{"playwright":{}}}\n', encoding="utf-8")
    (source_home / "version.json").write_text('{"version":"0.0.1"}\n', encoding="utf-8")
    (source_home / ".personality_migration").write_text("1\n", encoding="utf-8")
    (source_home / "state_5.sqlite").write_text("state", encoding="utf-8")
    (source_home / "state_5.sqlite-wal").write_text("state-wal", encoding="utf-8")
    (source_home / "skills").mkdir()
    (source_home / "skills" / "orche.txt").write_text("skill", encoding="utf-8")
    (source_home / "rules").mkdir()
    (source_home / "rules" / "default.rules").write_text("rule", encoding="utf-8")
    (source_home / "memories").mkdir()
    (source_home / "memories" / "memory.txt").write_text("memory", encoding="utf-8")
    (source_home / "hooks").mkdir()
    (source_home / "hooks" / "custom-hook.sh").write_text("#!/bin/sh\necho custom\n", encoding="utf-8")
    (source_home / "logs_1.sqlite").write_text("logs", encoding="utf-8")
    (source_home / "history.jsonl").write_text("history\n", encoding="utf-8")
    (source_home / "models_cache.json").write_text('{"models":[]}\n', encoding="utf-8")
    (source_home / "config.toml.orche.bak").write_text("backup\n", encoding="utf-8")
    (source_home / "log").mkdir()
    (source_home / "log" / "codex-tui.log").write_text("log\n", encoding="utf-8")
    (source_home / "sessions").mkdir()
    (source_home / "sessions" / "session.json").write_text("session\n", encoding="utf-8")
    (source_home / "shell_snapshots").mkdir()
    (source_home / "shell_snapshots" / "capture.sh").write_text("snapshot\n", encoding="utf-8")
    (source_home / ".tmp").mkdir()
    (source_home / ".tmp" / "cache.txt").write_text("tmp\n", encoding="utf-8")
    (source_home / "tmp").mkdir()
    (source_home / "tmp" / "cache.txt").write_text("tmp\n", encoding="utf-8")
    (source_home / "cache").mkdir()
    (source_home / "cache" / "entry.txt").write_text("cache\n", encoding="utf-8")

    target = Path(
        backend.ensure_managed_codex_home(
            "repo-codex-main",
            cwd=tmp_path,
            discord_channel_id=None,
        )
    )

    assert (target / "auth.json").read_text(encoding="utf-8") == '{"token":"auth"}\n'
    assert (target / "mcp.json").read_text(encoding="utf-8") == '{"mcpServers":{"playwright":{}}}\n'
    assert (target / "version.json").read_text(encoding="utf-8") == '{"version":"0.0.1"}\n'
    assert (target / ".personality_migration").read_text(encoding="utf-8") == "1\n"
    assert (target / "state_5.sqlite").read_text(encoding="utf-8") == "state"
    assert (target / "state_5.sqlite-wal").read_text(encoding="utf-8") == "state-wal"
    assert (target / "skills" / "orche.txt").read_text(encoding="utf-8") == "skill"
    assert (target / "rules" / "default.rules").read_text(encoding="utf-8") == "rule"
    assert (target / "memories" / "memory.txt").read_text(encoding="utf-8") == "memory"
    assert (target / "hooks" / "custom-hook.sh").read_text(encoding="utf-8") == "#!/bin/sh\necho custom\n"
    assert (target / "hooks" / "discord-turn-notify.sh").exists()
    assert not (target / "logs_1.sqlite").exists()
    assert not (target / "history.jsonl").exists()
    assert not (target / "models_cache.json").exists()
    assert not (target / "config.toml.orche.bak").exists()
    assert not (target / "log").exists()
    assert not (target / "sessions").exists()
    assert not (target / "shell_snapshots").exists()
    assert not (target / ".tmp").exists()
    assert not (target / "tmp").exists()
    assert not (target / "cache").exists()


def test_ensure_managed_codex_home_refreshes_hooks_json_from_source_home(xdg_runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "DEFAULT_CODEX_HOME_ROOT", tmp_path / "managed")
    monkeypatch.setattr(backend, "DEFAULT_CODEX_SOURCE_HOME", tmp_path / ".codex")
    monkeypatch.setattr(backend.codex_agent_module, "DEFAULT_RUNTIME_HOME_ROOT", tmp_path / "managed")
    monkeypatch.setattr(backend.codex_agent_module, "DEFAULT_CODEX_SOURCE_HOME", tmp_path / ".codex")

    source_home = tmp_path / ".codex"
    source_home.mkdir()
    (source_home / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")
    (source_home / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "/bin/echo source-stop-hook-v1",
                                }
                            ]
                        }
                    ]
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    target = Path(
        backend.ensure_managed_codex_home(
            "repo-codex-main",
            cwd=tmp_path,
            discord_channel_id=None,
        )
    )
    (target / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "/bin/echo stale-target-hook",
                                }
                            ]
                        }
                    ]
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (source_home / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "/bin/echo source-stop-hook-v2",
                                }
                            ]
                        }
                    ]
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    backend.ensure_managed_codex_home(
        "repo-codex-main",
        cwd=tmp_path,
        discord_channel_id=None,
    )

    hooks_payload = json.loads((target / "hooks.json").read_text(encoding="utf-8"))

    assert hooks_payload["hooks"]["Stop"][0]["hooks"][0]["command"] == "/bin/echo source-stop-hook-v2"
    rendered = json.dumps(hooks_payload)
    assert "stale-target-hook" not in rendered


def test_source_config_lock_acquires_and_releases(xdg_runtime, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    lock_path = locks_dir() / f"{SOURCE_CONFIG_LOCK_NAME}.lock"

    with source_config_lock():
        assert lock_path.exists()
        lines = lock_path.read_text(encoding="utf-8").splitlines()
        assert lines[0] == str(os.getpid())
        assert lines[1] == str(tmp_path.resolve())

    assert not lock_path.exists()


def test_source_config_lock_removes_stale_dead_pid(xdg_runtime, monkeypatch):
    lock_path = locks_dir() / f"{SOURCE_CONFIG_LOCK_NAME}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("424242\n/tmp/stale\n", encoding="utf-8")
    checked_pids: list[int] = []

    def fake_pid_is_alive(pid: int) -> bool:
        checked_pids.append(pid)
        return False

    monkeypatch.setattr("agents.codex._pid_is_alive", fake_pid_is_alive)

    with source_config_lock():
        lines = lock_path.read_text(encoding="utf-8").splitlines()
        assert lines[0] == str(os.getpid())

    assert checked_pids == [424242]
    assert not lock_path.exists()


def test_source_config_lock_invalid_pid_falls_back_to_timeout(xdg_runtime, monkeypatch):
    lock_path = locks_dir() / f"{SOURCE_CONFIG_LOCK_NAME}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("/tmp/legacy-cwd-only\n", encoding="utf-8")
    now = {"value": 100.0}
    checked_pids: list[int] = []

    monkeypatch.setattr("agents.codex.time.time", lambda: now["value"])

    def fake_sleep(seconds: float) -> None:
        now["value"] += seconds

    monkeypatch.setattr("agents.codex.time.sleep", fake_sleep)
    monkeypatch.setattr("agents.codex._pid_is_alive", lambda pid: checked_pids.append(pid) or True)

    with pytest.raises(RuntimeError, match="Timed out waiting for Codex source config lock"):
        with source_config_lock(timeout=0.15):
            pytest.fail("lock acquisition should time out for invalid legacy content")

    assert checked_pids == []
    assert lock_path.read_text(encoding="utf-8") == "/tmp/legacy-cwd-only\n"


def test_source_config_lock_living_pid_times_out(xdg_runtime, monkeypatch):
    lock_path = locks_dir() / f"{SOURCE_CONFIG_LOCK_NAME}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("31337\n/tmp/active\n", encoding="utf-8")
    now = {"value": 200.0}
    checked_pids: list[int] = []

    monkeypatch.setattr("agents.codex.time.time", lambda: now["value"])

    def fake_sleep(seconds: float) -> None:
        now["value"] += seconds

    monkeypatch.setattr("agents.codex.time.sleep", fake_sleep)

    def fake_pid_is_alive(pid: int) -> bool:
        checked_pids.append(pid)
        return True

    monkeypatch.setattr("agents.codex._pid_is_alive", fake_pid_is_alive)

    with pytest.raises(RuntimeError, match="Timed out waiting for Codex source config lock"):
        with source_config_lock(timeout=0.15):
            pytest.fail("lock acquisition should time out while owner is alive")

    assert checked_pids
    assert set(checked_pids) == {31337}
    assert lock_path.read_text(encoding="utf-8") == "31337\n/tmp/active\n"


def test_ensure_managed_claude_home_preserves_existing_source_config(xdg_runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(backend.claude_agent_module, "DEFAULT_RUNTIME_HOME_ROOT", tmp_path / "managed")
    monkeypatch.setattr(backend.claude_agent_module, "source_claude_config_path", lambda: tmp_path / ".claude.json")
    monkeypatch.setattr(backend.claude_agent_module, "source_claude_home_path", lambda: tmp_path / ".claude")
    monkeypatch.setattr(
        backend.claude_agent_module,
        "source_claude_config_backup_path",
        lambda: tmp_path / ".claude.json.orche.bak",
    )
    source_config_path = tmp_path / ".claude.json"
    source_config_path.write_text(
        json.dumps(
            {
                "numStartups": 12,
                "projects": {
                    str(tmp_path.resolve()): {
                        "allowedTools": ["Bash(git status:*)"],
                    }
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    backend.ensure_managed_claude_home(
        "repo-claude-main",
        cwd=tmp_path,
        discord_channel_id=None,
    )

    source_payload = json.loads(source_config_path.read_text(encoding="utf-8"))

    assert source_payload["numStartups"] == 12
    assert source_payload["projects"][str(tmp_path.resolve())]["allowedTools"] == ["Bash(git status:*)"]
    assert source_payload["projects"][str(tmp_path.resolve())]["hasTrustDialogAccepted"] is True


def test_ensure_managed_claude_home_uses_configured_source_config_path(xdg_runtime, tmp_path, monkeypatch):
    configured_path = tmp_path / "config" / "claude-custom.json"
    backend.save_config(
        {
            "_comment": "runtime",
            "claude_config_path": str(configured_path),
        }
    )
    monkeypatch.setattr(backend.claude_agent_module, "DEFAULT_RUNTIME_HOME_ROOT", tmp_path / "managed")
    monkeypatch.setattr(backend.claude_agent_module, "source_claude_home_path", lambda: tmp_path / ".claude")

    target = backend.ensure_managed_claude_home(
        "repo-claude-main",
        cwd=tmp_path,
        discord_channel_id=None,
    )

    assert Path(target).exists()
    source_payload = json.loads(configured_path.read_text(encoding="utf-8"))
    settings_payload = json.loads((Path(target) / "settings.json").read_text(encoding="utf-8"))
    assert source_payload["projects"][str(tmp_path.resolve())]["hasTrustDialogAccepted"] is True
    assert "--session repo-claude-main" in settings_payload["hooks"]["Stop"][0]["hooks"][0]["command"]


def test_ensure_managed_claude_home_uses_configured_source_home_path(xdg_runtime, tmp_path, monkeypatch):
    configured_home = tmp_path / "homes" / "claude-custom-home"
    configured_home.mkdir(parents=True)
    (configured_home / "settings.json").write_text('{"theme":"configured-home"}\n', encoding="utf-8")
    backend.save_config(
        {
            "_comment": "runtime",
            "claude_home_path": str(configured_home),
        }
    )
    monkeypatch.setattr(backend.claude_agent_module, "DEFAULT_RUNTIME_HOME_ROOT", tmp_path / "managed")
    monkeypatch.setattr(backend.claude_agent_module, "source_claude_config_path", lambda: tmp_path / ".claude.json")

    target = backend.ensure_managed_claude_home(
        "repo-claude-main",
        cwd=tmp_path,
        discord_channel_id=None,
    )

    assert not (Path(target) / "workspace.json").exists()
    settings_payload = json.loads((Path(target) / "settings.json").read_text(encoding="utf-8"))
    assert settings_payload["theme"] == "configured-home"


def test_ensure_session_supports_claude_agent(xdg_runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(backend.claude_agent_module, "DEFAULT_RUNTIME_HOME_ROOT", tmp_path / "managed")
    monkeypatch.setattr(backend.claude_agent_module, "source_claude_config_path", lambda: tmp_path / ".claude.json")
    monkeypatch.setattr(backend.claude_agent_module, "source_claude_home_path", lambda: tmp_path / ".claude")
    monkeypatch.setattr(
        backend.claude_agent_module,
        "source_claude_config_backup_path",
        lambda: tmp_path / ".claude.json.orche.bak",
    )
    (tmp_path / ".claude").mkdir()
    monkeypatch.setattr(backend, "ensure_pane", lambda session, cwd, agent, **kwargs: "%7")
    monkeypatch.setattr(backend, "ensure_agent_running", lambda *args, **kwargs: "%7")
    monkeypatch.setattr(
        backend,
        "wait_for_managed_startup_ready",
        lambda session, plugin, pane_id, cwd, timeout=backend.STARTUP_TIMEOUT: pane_id,
    )

    pane_id = backend.ensure_session(
        "demo-claude",
        tmp_path,
        "claude",
        notify_to="discord",
        notify_target="123",
    )
    meta = backend.load_meta("demo-claude")

    assert pane_id == "%7"
    assert meta["agent"] == "claude"
    assert meta["runtime_home"].endswith("demo-claude")
    assert meta["runtime_label"] == "Claude settings"
    assert meta["notify_binding"]["provider"] == "discord"


def test_ensure_session_waits_for_managed_claude_startup_hook(xdg_runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(backend.claude_agent_module, "DEFAULT_RUNTIME_HOME_ROOT", tmp_path / "managed")
    monkeypatch.setattr(backend.claude_agent_module, "source_claude_config_path", lambda: tmp_path / ".claude.json")
    monkeypatch.setattr(backend.claude_agent_module, "source_claude_home_path", lambda: tmp_path / ".claude")
    monkeypatch.setattr(
        backend.claude_agent_module,
        "source_claude_config_backup_path",
        lambda: tmp_path / ".claude.json.orche.bak",
    )
    (tmp_path / ".claude").mkdir()
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(backend, "ensure_pane", lambda session, cwd, agent, **kwargs: "%7")
    monkeypatch.setattr(backend, "ensure_agent_running", lambda *args, **kwargs: "%7")
    monkeypatch.setattr(
        backend,
        "wait_for_managed_startup_ready",
        lambda session, plugin, pane_id, cwd, timeout=backend.STARTUP_TIMEOUT: calls.append((session, pane_id)) or pane_id,
    )

    pane_id = backend.ensure_session(
        "demo-claude",
        tmp_path,
        "claude",
        notify_to="discord",
        notify_target="123",
    )

    assert pane_id == "%7"
    assert calls == [("demo-claude", "%7")]
    assert backend.load_meta("demo-claude")["startup"]["state"] == "launching"


def test_ensure_session_waits_for_managed_codex_startup_hook(xdg_runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "DEFAULT_CODEX_HOME_ROOT", tmp_path / "managed")
    monkeypatch.setattr(backend, "DEFAULT_CODEX_SOURCE_HOME", tmp_path / ".codex")
    monkeypatch.setattr(backend.codex_agent_module, "DEFAULT_RUNTIME_HOME_ROOT", tmp_path / "managed")
    monkeypatch.setattr(backend.codex_agent_module, "DEFAULT_CODEX_SOURCE_HOME", tmp_path / ".codex")
    (tmp_path / ".codex").mkdir()
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(backend, "ensure_pane", lambda session, cwd, agent, **kwargs: "%9")
    monkeypatch.setattr(backend, "ensure_agent_running", lambda *args, **kwargs: "%9")
    monkeypatch.setattr(
        backend,
        "wait_for_managed_startup_ready",
        lambda session, plugin, pane_id, cwd, timeout=backend.STARTUP_TIMEOUT: calls.append((session, pane_id)) or pane_id,
    )

    pane_id = backend.ensure_session(
        "demo-codex",
        tmp_path,
        "codex",
        notify_to="discord",
        notify_target="123",
    )

    assert pane_id == "%9"
    assert calls == [("demo-codex", "%9")]
    assert backend.load_meta("demo-codex")["startup"]["state"] == "launching"


def test_ensure_session_rejects_reusing_managed_codex_session_after_startup_timeout(xdg_runtime, tmp_path, monkeypatch):
    session = "demo-codex-timeout"
    backend.save_meta(
        session,
        {
            "session": session,
            "agent": "codex",
            "launch_mode": "managed",
            "runtime_home": str(tmp_path / "managed" / session),
            "runtime_home_managed": True,
            "startup": {
                "state": "timeout",
                "blocked_reason": "Timed out waiting for Codex SessionStart(startup) hook in %9",
            },
        },
    )
    monkeypatch.setattr(
        backend,
        "prepare_managed_runtime",
        lambda plugin, session, cwd, discord_channel_id: backend.AgentRuntime(
            home=str(tmp_path / "managed" / session),
            managed=True,
            label=plugin.runtime_label,
        ),
    )
    monkeypatch.setattr(backend, "ensure_pane", lambda session, cwd, agent, **kwargs: "%9")
    monkeypatch.setattr(backend, "is_agent_running", lambda plugin, pane_id: True)
    monkeypatch.setattr(
        backend,
        "ensure_agent_running",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("ensure_agent_running should not be called")),
    )
    monkeypatch.setattr(
        backend,
        "wait_for_managed_startup_ready",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("wait_for_managed_startup_ready should not be called")),
    )

    with pytest.raises(backend.OrcheError, match="Session demo-codex-timeout is not ready because Timed out waiting for Codex SessionStart\\(startup\\) hook in %9"):
        backend.ensure_session(
            session,
            tmp_path,
            "codex",
            notify_to="discord",
            notify_target="123",
        )

    assert backend.load_meta(session)["startup"]["state"] == "timeout"


def test_ensure_native_session_supports_claude_agent_and_stores_native_args(xdg_runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "ensure_pane", lambda session, cwd, agent: "%8")
    monkeypatch.setattr(backend, "ensure_native_agent_running", lambda *args, **kwargs: "%8")

    pane_id = backend.ensure_native_session(
        "demo-claude-native",
        tmp_path,
        "claude",
        cli_args=["--print", "--help"],
    )
    meta = backend.load_meta("demo-claude-native")

    assert pane_id == "%8"
    assert meta["agent"] == "claude"
    assert meta["launch_mode"] == "native"
    assert meta["native_cli_args"] == ["--print", "--help"]
    assert meta["runtime_home"] == ""


def test_claude_agent_matches_node_frontend_process():
    plugin = backend.ClaudeAgent()

    assert plugin.matches_process("node", [])
    assert plugin.matches_process("bash", ["node /opt/homebrew/bin/claude"])


def test_get_agent_applies_configured_claude_command_and_process_match(xdg_runtime):
    backend.save_config(
        {
            "_comment": "runtime",
            "claude_command": "/opt/tools/claude-wrapper",
        }
    )

    plugin = backend.get_agent("claude")
    launch_command = backend.build_native_agent_launch_command(
        plugin,
        session="demo-claude",
        cwd=Path("/tmp/repo"),
        cli_args=(),
    )

    assert plugin.matches_process("claude-wrapper", [])
    assert "command -v /opt/tools/claude-wrapper" in launch_command
    assert "exec /opt/tools/claude-wrapper --dangerously-skip-permissions" in launch_command


def test_get_agent_applies_configured_claude_home_and_config_paths(xdg_runtime):
    backend.save_config(
        {
            "_comment": "runtime",
            "claude_home_path": "/opt/tools/claude-home",
            "claude_config_path": "/opt/tools/claude.json",
        }
    )

    backend.get_agent("claude")

    assert backend.claude_agent_module.DEFAULT_CLAUDE_SOURCE_HOME == Path("/opt/tools/claude-home")
    assert backend.claude_agent_module.DEFAULT_CLAUDE_SOURCE_CONFIG_PATH == Path("/opt/tools/claude.json")


def test_claude_managed_launch_command_uses_runtime_settings_without_overriding_home(xdg_runtime, tmp_path):
    backend.save_config(
        {
            "_comment": "runtime",
            "claude_command": "/opt/tools/claude-wrapper",
        }
    )

    plugin = backend.get_agent("claude")
    runtime = backend.AgentRuntime(home=str(tmp_path / "managed-home"), managed=True, label=plugin.runtime_label)
    launch_command = plugin.build_launch_command(
        session="demo-claude",
        cwd=Path("/tmp/repo"),
        runtime=runtime,
        discord_channel_id=None,
        approve_all=True,
    )

    assert f"export HOME={tmp_path / 'managed-home'}" not in launch_command
    assert "--setting-sources user" in launch_command
    assert f"--settings {tmp_path / 'managed-home' / 'settings.json'}" in launch_command
    assert "exec /opt/tools/claude-wrapper --dangerously-skip-permissions" in launch_command


def test_claude_completion_summary_requires_returned_prompt():
    plugin = backend.ClaudeAgent()
    capture = (
        "before prompt\n"
        "❯ Implement the parser refactor.\n"
        "\n"
        "⏺ Updated parser.py and parser_test.py\n"
        "\n"
        "✻ Working…\n"
    )

    summary = plugin.extract_completion_summary(capture, "Implement the parser refactor.")

    assert summary == ""


def test_orche_shim_executes_repo_cli(xdg_runtime):
    shim = backend.ensure_orche_shim()

    assert shim.exists()
    content = shim.read_text(encoding="utf-8")

    assert "sys.path.insert(0," in content
    assert str(Path(backend.__file__).resolve().parent) in content


def test_orche_shim_executes_current_orche_binary_when_available(xdg_runtime, monkeypatch, tmp_path):
    binary = tmp_path / "orche"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)

    monkeypatch.setattr(sys, "argv", [str(binary)])

    shim = backend.ensure_orche_shim()
    content = shim.read_text(encoding="utf-8")

    assert f'exec {binary.resolve()} "$@"' in content
    assert "sys.path.insert(0," not in content


def test_orche_bootstrap_command_prefers_current_binary(xdg_runtime, monkeypatch, tmp_path):
    binary = tmp_path / "orche"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)

    monkeypatch.setattr(sys, "argv", [str(binary)])

    command = backend._orche_bootstrap_command()

    assert command == [str(binary.resolve())]


def test_build_native_agent_launch_command_checks_cli_presence(xdg_runtime, tmp_path):
    plugin = backend.get_agent("codex")

    command = backend.build_native_agent_launch_command(
        plugin,
        session="demo-codex",
        cwd=tmp_path,
        cli_args=["--model", "gpt-5.4"],
    )

    assert "command -v codex" in command
    assert "orche launch error: Codex CLI not found in PATH." in command
    assert (
        f"exec codex --no-alt-screen -C {tmp_path} --dangerously-bypass-approvals-and-sandbox --model gpt-5.4"
        in command
    )


def test_codex_managed_launch_command_enables_hooks(xdg_runtime, tmp_path):
    plugin = backend.get_agent("codex")
    runtime = backend.AgentRuntime(home=str(tmp_path / "managed-home"), managed=True, label=plugin.runtime_label)

    command = plugin.build_launch_command(
        session="demo-codex",
        cwd=tmp_path,
        runtime=runtime,
        discord_channel_id=None,
        approve_all=True,
    )

    assert f"export CODEX_HOME={tmp_path / 'managed-home'}" in command
    assert "exec codex --enable codex_hooks --no-alt-screen" in command


def test_get_pane_info_reads_exact_target_pane(xdg_runtime, monkeypatch):
    monkeypatch.setattr(backend, "pane_exists", lambda pane_id: pane_id == "%7")
    monkeypatch.setattr(
        backend,
        "_tmux_value_for_pane",
        lambda pane_id, fmt: (
            "orche-reviewer\t%7\t@3\tmain\t0\t12345\tzsh\t/tmp/project\tdemo-worker"
            if pane_id == "%7"
            else ""
        ),
    )

    info = backend.get_pane_info("%7")

    assert info == {
        "session_name": "orche-reviewer",
        "pane_id": "%7",
        "window_id": "@3",
        "window_name": "main",
        "pane_dead": "0",
        "pane_pid": "12345",
        "pane_current_command": "zsh",
        "pane_current_path": "/tmp/project",
        "pane_title": "demo-worker",
    }


def test_pending_turn_completion_summary_falls_back_to_full_capture_when_delta_clips_prompt():
    plugin = backend.get_agent("codex")
    prompt = "Reply with exactly OK42"
    capture = (
        "› Reply with exactly\n"
        "  OK42\n"
        "\n"
        "• OK42\n"
        "\n"
        "› Implement {feature}\n"
    )

    summary = backend._pending_turn_completion_summary(
        plugin,
        pending_turn={
            "before_capture": "› ",
            "prompt": prompt,
        },
        capture=capture,
    )

    assert summary == "OK42"


def test_run_session_watchdog_does_not_complete_turn_when_capture_shows_completion(xdg_runtime, tmp_path, monkeypatch):
    session = "demo-inline-watchdog"
    backend.save_meta(
        session,
        {
            "session": session,
            "cwd": str(tmp_path),
            "agent": "codex",
            "pane_id": "%7",
            "pending_turn": {
                "turn_id": "turn-1",
                "prompt": "Reply with exactly OK42",
                "before_capture": "› ",
                "submitted_at": 1.0,
                "pane_id": "%7",
                "notifications": {},
                "watchdog": {},
            },
        },
    )
    capture = (
        "› Reply with exactly\n"
        "  OK42\n"
        "\n"
        "• OK42\n"
        "\n"
        "› Implement {feature}\n"
    )
    emitted = []
    sleep_calls = []

    monkeypatch.setattr(
        backend,
        "sample_watchdog_state",
        lambda session, pane_id="": {
            "capture": capture,
            "signature": "sig",
            "cursor_x": "1",
            "cursor_y": "1",
            "cpu_percent": 0.0,
            "agent_running": True,
        },
    )
    monkeypatch.setattr(backend, "emit_internal_notify", lambda *args, **kwargs: emitted.append(kwargs) or False)
    original_sleep = backend.time.sleep

    def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        if len(sleep_calls) == 2:
            meta = backend.load_meta(session)
            meta.pop("pending_turn", None)
            backend.save_meta(session, meta)
        original_sleep(0)

    monkeypatch.setattr(backend.time, "sleep", fake_sleep)

    result = backend.run_session_watchdog(session, turn_id="turn-1", poll_interval=0.01, notify_buffer=0.0)
    meta = backend.load_meta(session)

    assert result == "completed"
    assert emitted == []
    assert "pending_turn" not in meta
    assert "last_completed_turn" not in meta


def test_wait_for_agent_process_start_surfaces_explicit_launch_error(monkeypatch):
    plugin = backend.get_agent("codex")
    capture = "orche launch error: Codex CLI not found in PATH. Install codex or add it to PATH."

    monkeypatch.setattr(backend, "read_pane", lambda pane_id, lines=backend.DEFAULT_CAPTURE_LINES: capture)
    monkeypatch.setattr(backend, "get_pane_info", lambda pane_id: {"pane_dead": "0"})
    monkeypatch.setattr(backend, "is_agent_running", lambda plugin, pane_id: False)

    with pytest.raises(backend.OrcheError, match="Codex CLI not found in PATH"):
        backend.wait_for_agent_process_start(plugin, "%1", timeout=0.1)


def test_wait_for_agent_process_start_rejects_plain_shell_prompt(monkeypatch):
    plugin = backend.get_agent("codex")
    monkeypatch.setattr(backend, "read_pane", lambda pane_id, lines=backend.DEFAULT_CAPTURE_LINES: "dnq@host repo %")
    monkeypatch.setattr(backend, "get_pane_info", lambda pane_id: {"pane_dead": "0"})
    monkeypatch.setattr(backend, "is_agent_running", lambda plugin, pane_id: False)

    with pytest.raises(backend.OrcheError, match="Timed out waiting for Codex process to start"):
        backend.wait_for_agent_process_start(plugin, "%1", timeout=0.1)


def test_wait_for_managed_startup_ready_falls_back_to_codex_ready_surface(xdg_runtime, tmp_path, monkeypatch):
    session = "demo-codex-startup-fallback"
    backend.save_meta(session, {"session": session, "startup": {"state": "launching"}})
    plugin = backend.get_agent("codex")
    capture = (
        "OpenAI Codex\n"
        f"directory: {tmp_path}\n"
        "Esc to interrupt\n"
    )

    monkeypatch.setattr(backend, "read_pane", lambda pane_id, lines=backend.DEFAULT_CAPTURE_LINES: capture)
    monkeypatch.setattr(backend, "get_pane_info", lambda pane_id: {"pane_dead": "0"})
    monkeypatch.setattr(backend, "is_agent_running", lambda plugin, pane_id: True)

    pane_id = backend.wait_for_managed_startup_ready(session, plugin, "%1", tmp_path, timeout=1.5)

    assert pane_id == "%1"
    assert backend.load_meta(session)["startup"]["state"] == "ready"
    assert backend.load_meta(session)["startup"]["ready_source"] == "ready-surface-fallback"


def test_wait_for_managed_startup_ready_applies_claude_grace_period(xdg_runtime, tmp_path, monkeypatch):
    session = "demo-claude-startup-grace"
    backend.save_meta(
        session,
        {
            "session": session,
            "startup": {
                "state": "ready",
                "ready_at": 100.0,
                "updated_at": 100.0,
            },
        },
    )
    plugin = backend.get_agent("claude")
    now = {"value": 100.0}
    sleeps: list[float] = []

    monkeypatch.setattr(backend.time, "time", lambda: now["value"])

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now["value"] += seconds

    monkeypatch.setattr(backend.time, "sleep", fake_sleep)

    pane_id = backend.wait_for_managed_startup_ready(session, plugin, "%1", tmp_path, timeout=5.0)

    assert pane_id == "%1"
    assert sum(sleeps) >= backend.CLAUDE_STARTUP_GRACE_SECONDS


def test_wait_for_managed_startup_ready_rejects_timeout_state_immediately(xdg_runtime, tmp_path):
    session = "demo-codex-startup-timeout"
    plugin = backend.get_agent("codex")
    backend.save_meta(
        session,
        {
            "session": session,
            "startup": {
                "state": "timeout",
                "blocked_reason": "Timed out waiting for Codex SessionStart(startup) hook in %1",
            },
        },
    )

    with pytest.raises(backend.AgentStartupBlockedError, match="Timed out waiting for Codex SessionStart\\(startup\\) hook in %1"):
        backend.wait_for_managed_startup_ready(session, plugin, "%1", tmp_path, timeout=1.0)


def test_wait_for_prompt_ack_accepts_last_completed_turn(xdg_runtime, monkeypatch):
    session = "demo-claude-prompt-ack"
    backend.save_meta(
        session,
        {
            "session": session,
            "pending_turn": {
                "turn_id": "turn-1",
                "prompt": "hello",
                "prompt_ack": {
                    "state": "pending",
                    "accepted_at": 0.0,
                    "source": "",
                },
            },
        },
    )
    now = {"value": 0.0}

    monkeypatch.setattr(backend.time, "time", lambda: now["value"])

    def fake_sleep(seconds: float) -> None:
        now["value"] += seconds
        meta = backend.load_meta(session)
        pending_turn = dict(meta["pending_turn"])
        pending_turn["prompt_ack"] = {
            "state": "accepted",
            "accepted_at": now["value"],
            "source": "user-prompt-submit",
        }
        meta["last_completed_turn"] = pending_turn
        meta.pop("pending_turn", None)
        backend.save_meta(session, meta)

    monkeypatch.setattr(backend.time, "sleep", fake_sleep)

    prompt_ack = backend.wait_for_prompt_ack(session, turn_id="turn-1", prompt="hello", timeout=1.0)

    assert prompt_ack["state"] == "accepted"
    assert prompt_ack["source"] == "user-prompt-submit"


def test_codex_submit_prompt_waits_before_enter(monkeypatch):
    plugin = CodexAgent()
    bridge = FakeBridge()
    sleeps: list[float] = []

    monkeypatch.setattr("agents.codex.time.sleep", lambda seconds: sleeps.append(seconds))

    plugin.submit_prompt("demo-codex", "Reply with exactly DEBUG_TOKEN", bridge=bridge)

    assert bridge.calls == [
        ("type", "demo-codex", "Reply with exactly DEBUG_TOKEN"),
        ("keys", "demo-codex", ["Enter"]),
    ]
    assert sleeps == [codex_submit_settle_seconds("Reply with exactly DEBUG_TOKEN")]


def test_claude_submit_prompt_waits_before_enter(monkeypatch):
    plugin = ClaudeAgent()
    bridge = FakeBridge()
    sleeps: list[float] = []

    monkeypatch.setattr("agents.claude.time.sleep", lambda seconds: sleeps.append(seconds))

    plugin.submit_prompt("demo-claude", "Reply with exactly DEBUG_TOKEN", bridge=bridge)

    assert bridge.calls == [
        ("type", "demo-claude", "Reply with exactly DEBUG_TOKEN"),
        ("keys", "demo-claude", ["Enter"]),
    ]
    assert sleeps == [CLAUDE_SUBMIT_SETTLE_SECONDS]


def test_send_prompt_waits_for_managed_claude_prompt_ack(xdg_runtime, tmp_path, monkeypatch):
    session = "demo-claude-managed"
    backend.save_meta(
        session,
        {
            "session": session,
            "agent": "claude",
            "launch_mode": "managed",
            "runtime_home_managed": True,
        },
    )
    bridge = FakeBridge()
    captured: dict[str, object] = {}

    monkeypatch.setattr(backend, "BRIDGE", bridge)
    monkeypatch.setattr(backend, "ensure_session", lambda *args, **kwargs: "%7")
    monkeypatch.setattr(backend, "read_pane", lambda *args, **kwargs: "Claude Code")
    monkeypatch.setattr(backend, "start_session_watchdog", lambda *args, **kwargs: None)
    monkeypatch.setattr(backend, "append_action_history", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        backend,
        "wait_for_prompt_ack",
        lambda ack_session, **kwargs: captured.update({"session": ack_session, **kwargs}) or {"state": "accepted"},
    )

    pane_id = backend.send_prompt(session, tmp_path, "claude", "hello")

    assert pane_id == "%7"
    assert bridge.calls == [
        ("type", session, "hello"),
        ("keys", session, ["Enter"]),
    ]
    assert captured["session"] == session
    assert captured["prompt"] == "hello"
    assert captured["turn_id"]


def test_send_prompt_reuses_supplied_pane_id_without_ensuring_session(xdg_runtime, tmp_path, monkeypatch):
    session = "demo-codex-managed"
    backend.save_meta(
        session,
        {
            "session": session,
            "agent": "codex",
            "launch_mode": "managed",
            "runtime_home_managed": True,
        },
    )
    bridge = FakeBridge()

    monkeypatch.setattr(backend, "BRIDGE", bridge)
    monkeypatch.setattr(
        backend,
        "ensure_session",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("ensure_session should not be called")),
    )
    monkeypatch.setattr(
        backend,
        "ensure_native_session",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("ensure_native_session should not be called")),
    )
    monkeypatch.setattr(backend, "read_pane", lambda pane_id, *args, **kwargs: f"capture:{pane_id}")
    monkeypatch.setattr(backend, "start_session_watchdog", lambda *args, **kwargs: None)
    monkeypatch.setattr(backend, "append_action_history", lambda *args, **kwargs: None)

    pane_id = backend.send_prompt(session, tmp_path, "codex", "hello", pane_id="%42")

    assert pane_id == "%42"
    assert bridge.calls == [
        ("type", session, "hello"),
        ("keys", session, ["Enter"]),
    ]
    pending_turn = backend.load_meta(session)["pending_turn"]
    assert pending_turn["pane_id"] == "%42"
    assert pending_turn["before_capture"] == "capture:%42"


def test_codex_submit_prompt_skips_delay_for_empty_prompt(monkeypatch):
    plugin = CodexAgent()
    bridge = FakeBridge()
    sleeps: list[float] = []

    monkeypatch.setattr("agents.codex.time.sleep", lambda seconds: sleeps.append(seconds))

    plugin.submit_prompt("demo-codex", "", bridge=bridge)

    assert bridge.calls == [("keys", "demo-codex", ["Enter"])]
    assert sleeps == []


def test_codex_submit_prompt_delay_scales_with_prompt_length():
    assert codex_submit_settle_seconds("short") == CODEX_SUBMIT_SETTLE_MIN_SECONDS
    assert codex_submit_settle_seconds("x" * 200) == CODEX_SUBMIT_SETTLE_MAX_SECONDS


def test_ensure_native_agent_running_uses_respawn_pane_without_send_keys(xdg_runtime, tmp_path, monkeypatch):
    plugin = backend.get_agent("codex")
    tmux_calls = []

    monkeypatch.setattr(backend, "is_agent_running", lambda plugin, pane_id: False)
    monkeypatch.setattr(backend, "get_pane_info", lambda pane_id: {"pane_dead": "0"})
    monkeypatch.setattr(backend, "wait_for_agent_process_start", lambda plugin, pane_id: pane_id)
    monkeypatch.setattr(backend, "bridge_name_pane", lambda pane_id, session: None)

    def fake_tmux(*args, **kwargs):
        tmux_calls.append(args)
        class Result:
            returncode = 0
            stdout = ""
            stderr = ""
        return Result()

    monkeypatch.setattr(backend, "tmux", fake_tmux)

    pane_id = backend.ensure_native_agent_running(
        plugin,
        "demo-codex",
        tmp_path,
        "%9",
        cli_args=["--model", "gpt-5.4"],
    )

    assert pane_id == "%9"
    assert any(call[:4] == ("respawn-pane", "-k", "-t", "%9") for call in tmux_calls)
    assert not any(call and call[0] == "send-keys" for call in tmux_calls)


def test_bridge_name_pane_uses_single_tmux_round_trip(monkeypatch):
    tmux_calls: list[tuple[str, ...]] = []

    def fake_tmux(*args, **kwargs):
        tmux_calls.append(tuple(str(value) for value in args))
        return subprocess.CompletedProcess(["tmux", *args], 0, "", "")

    monkeypatch.setattr(backend, "tmux", fake_tmux)

    backend.bridge_name_pane("%42", "demo-session")

    assert tmux_calls == [("select-pane", "-t", "%42", "-T", "demo-session")]


def test_ensure_pane_inline_mode_splits_current_tmux_session(xdg_runtime, tmp_path, monkeypatch):
    tmux_calls = []

    monkeypatch.setattr(backend, "bridge_name_pane", lambda pane_id, session: None)
    monkeypatch.setattr(backend, "pane_exists", lambda pane_id: pane_id == "%1")
    monkeypatch.setattr(
        backend,
        "get_pane_info",
        lambda pane_id: {
            "%1": {
                "pane_id": "%1",
                "session_name": "orche-reviewer",
                "window_id": "@1",
                "window_name": "main",
                "pane_dead": "0",
            },
            "%11": {
                "pane_id": "%11",
                "session_name": "orche-reviewer",
                "window_id": "@1",
                "window_name": "main",
                "pane_dead": "0",
            },
        }.get(pane_id),
    )

    def fake_tmux(*args, **kwargs):
        tmux_calls.append(args)
        if args[:2] == ("new-window", "-d"):
            return subprocess.CompletedProcess(
                ["tmux", *args],
                0,
                f"orche-reviewer{backend.TMUX_PANE_OUTPUT_SEPARATOR}%11{backend.TMUX_PANE_OUTPUT_SEPARATOR}"
                f"@3{backend.TMUX_PANE_OUTPUT_SEPARATOR}main\n",
                "",
            )
        return subprocess.CompletedProcess(["tmux", *args], 0, "", "")

    monkeypatch.setattr(backend, "tmux", fake_tmux)

    pane_id = backend.ensure_pane(
        "demo-inline-worker",
        tmp_path,
        "codex",
        tmux_mode="inline-pane",
        host_pane_id="%1",
        tmux_host_session="orche-reviewer",
    )

    meta = backend.load_meta("demo-inline-worker")

    assert pane_id == "%11"
    assert meta["tmux_mode"] == "inline-pane"
    assert meta["host_pane_id"] == "%1"
    assert meta["tmux_host_session"] == "orche-reviewer"
    assert any(
        call[:4] == ("new-window", "-d", "-t", "orche-reviewer:0")
        for call in tmux_calls
    )
    assert any(
        call[:8] == ("join-pane", "-d", "-h", "-l", "25%", "-s", "%11", "-t")
        for call in tmux_calls
    )


def test_create_temp_inline_pane_targets_next_window_index_and_retries_on_conflict(
    xdg_runtime, tmp_path, monkeypatch
):
    tmux_calls = []
    listed_indexes = iter(["0\n", "0\n1\n"])

    def fake_tmux(*args, **kwargs):
        tmux_calls.append(args)
        if args[:4] == ("list-windows", "-t", "orche-reviewer", "-F"):
            return subprocess.CompletedProcess(["tmux", *args], 0, next(listed_indexes), "")
        if args[:4] == ("new-window", "-d", "-t", "orche-reviewer:1"):
            raise subprocess.CalledProcessError(
                1,
                ["tmux", *args],
                output="",
                stderr="create window failed: index 1 in use",
            )
        if args[:4] == ("new-window", "-d", "-t", "orche-reviewer:2"):
            return subprocess.CompletedProcess(
                ["tmux", *args],
                0,
                f"orche-reviewer{backend.TMUX_PANE_OUTPUT_SEPARATOR}%12{backend.TMUX_PANE_OUTPUT_SEPARATOR}"
                f"@4{backend.TMUX_PANE_OUTPUT_SEPARATOR}main\n",
                "",
            )
        return subprocess.CompletedProcess(["tmux", *args], 0, "", "")

    monkeypatch.setattr(backend, "tmux", fake_tmux)

    pane = backend._create_temp_inline_pane(tmux_session="orche-reviewer", cwd=tmp_path)

    assert pane["pane_id"] == "%12"
    assert (
        ("new-window", "-d", "-t", "orche-reviewer:1", "-c", str(tmp_path), "-P", "-F",
         f"#{'{'}session_name{'}'}{backend.TMUX_PANE_OUTPUT_SEPARATOR}"
         f"#{'{'}pane_id{'}'}{backend.TMUX_PANE_OUTPUT_SEPARATOR}"
         f"#{'{'}window_id{'}'}{backend.TMUX_PANE_OUTPUT_SEPARATOR}"
         f"#{'{'}window_name{'}'}")
        in tmux_calls
    )
    assert any(call[:4] == ("new-window", "-d", "-t", "orche-reviewer:2") for call in tmux_calls)


def test_ensure_pane_inline_mode_serializes_host_creation_until_meta_is_saved(
    xdg_runtime, tmp_path, monkeypatch
):
    original_save_meta = backend.save_meta
    first_started = threading.Event()
    errors: list[BaseException] = []
    second_observed_first_meta: dict[str, bool] = {}
    state_lock = threading.Lock()
    active_creates = 0
    max_active_creates = 0

    monkeypatch.setattr(backend, "bridge_name_pane", lambda pane_id, session: None)
    monkeypatch.setattr(
        backend,
        "tmux",
        lambda *args, **kwargs: subprocess.CompletedProcess(["tmux", *args], 0, "", ""),
    )

    def fake_create_inline_pane(session, cwd, *, tmux_session, host_pane_id=""):
        nonlocal active_creates, max_active_creates
        pane_id = "%11" if session == "worker-1" else "%12"
        with state_lock:
            active_creates += 1
            max_active_creates = max(max_active_creates, active_creates)
            if session == "worker-1":
                first_started.set()
            elif session == "worker-2":
                second_observed_first_meta["seen"] = bool(backend.load_meta("worker-1").get("pane_id"))
        time.sleep(0.05)
        with state_lock:
            active_creates -= 1
        return (
            {
                "pane_id": pane_id,
                "session_name": "orche-reviewer",
                "window_id": f"@{pane_id[1:]}",
                "window_name": "main",
                "pane_dead": "0",
                "inline_slot": "0" if session == "worker-1" else "1",
            },
            "%host",
        )

    def delayed_save_meta(session, meta):
        if session == "worker-1" and meta.get("pane_id") == "%11":
            time.sleep(0.15)
        return original_save_meta(session, meta)

    monkeypatch.setattr(backend, "create_inline_pane", fake_create_inline_pane)
    monkeypatch.setattr(backend, "save_meta", delayed_save_meta)

    def worker(session_name: str) -> None:
        try:
            backend.ensure_pane(
                session_name,
                tmp_path,
                "codex",
                tmux_mode="inline-pane",
                host_pane_id="%host",
                tmux_host_session="orche-reviewer",
            )
        except BaseException as exc:
            errors.append(exc)

    thread_one = threading.Thread(target=worker, args=("worker-1",))
    thread_two = threading.Thread(target=worker, args=("worker-2",))

    thread_one.start()
    assert first_started.wait(timeout=1.0)
    thread_two.start()
    thread_one.join(timeout=2.0)
    thread_two.join(timeout=2.0)

    assert not errors
    assert max_active_creates == 1
    assert second_observed_first_meta["seen"] is True
    assert backend.load_meta("worker-1")["host_pane_id"] == "%host"
    assert backend.load_meta("worker-2")["host_pane_id"] == "%host"


def test_ensure_pane_dedicated_mode_uses_new_session_output_for_new_sessions(xdg_runtime, tmp_path, monkeypatch):
    tmux_calls = []
    expected_tmux_session = backend.tmux_session_name("demo-dedicated-worker")

    monkeypatch.setattr(backend, "bridge_name_pane", lambda pane_id, session: None)
    monkeypatch.setattr(
        backend,
        "list_panes",
        lambda target=None: (_ for _ in ()).throw(AssertionError("list_panes should not be used for a fresh dedicated session")),
    )

    def fake_tmux(*args, **kwargs):
        tmux_calls.append(args)
        if list(args) == ["has-session", "-t", expected_tmux_session]:
            return subprocess.CompletedProcess(["tmux", *args], 1, "", "")
        if list(args[:4]) == ["new-session", "-d", "-s", expected_tmux_session]:
            return subprocess.CompletedProcess(
                ["tmux", *args],
                0,
                f"{expected_tmux_session}{backend.TMUX_PANE_OUTPUT_SEPARATOR}%12"
                f"{backend.TMUX_PANE_OUTPUT_SEPARATOR}@4{backend.TMUX_PANE_OUTPUT_SEPARATOR}"
                "orche-demo-dedicated-worker\n",
                "",
            )
        return subprocess.CompletedProcess(["tmux", *args], 0, "", "")

    monkeypatch.setattr(backend, "tmux", fake_tmux)

    pane_id = backend.ensure_pane("demo-dedicated-worker", tmp_path, "codex")

    meta = backend.load_meta("demo-dedicated-worker")

    assert pane_id == "%12"
    assert meta["tmux_session"] == expected_tmux_session
    assert meta["window_name"] == "orche-demo-dedicated-worker"
    assert any(call[:4] == ("new-session", "-d", "-s", expected_tmux_session) for call in tmux_calls)


def test_ensure_session_uses_inline_pane_for_tmux_notify_targeting_current_session(xdg_runtime, tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        backend,
        "prepare_managed_runtime",
        lambda plugin, session, *, cwd, discord_channel_id: backend.AgentRuntime(
            home=str(tmp_path / session),
            managed=True,
            label=plugin.runtime_label,
        ),
    )
    monkeypatch.setattr(backend, "current_session_id", lambda: "repo-reviewer")
    monkeypatch.setattr(
        backend,
        "_current_tmux_value",
        lambda fmt: {
            "#{session_name}": "orche-reviewer",
            "#{pane_id}": "%2",
        }.get(fmt, ""),
    )

    def fake_ensure_pane(session, cwd, agent, **kwargs):
        captured.update(kwargs)
        return "%7"

    monkeypatch.setattr(backend, "ensure_pane", fake_ensure_pane)
    monkeypatch.setattr(backend, "ensure_agent_running", lambda *args, **kwargs: "%7")
    monkeypatch.setattr(
        backend,
        "wait_for_managed_startup_ready",
        lambda session, plugin, pane_id, cwd, timeout=backend.STARTUP_TIMEOUT: pane_id,
    )

    pane_id = backend.ensure_session(
        "repo-worker",
        tmp_path,
        "codex",
        notify_to="tmux-bridge",
        notify_target="repo-reviewer",
    )

    assert pane_id == "%7"
    assert captured["tmux_mode"] == "inline-pane"
    assert captured["host_pane_id"] == "%2"
    assert captured["tmux_host_session"] == "orche-reviewer"
