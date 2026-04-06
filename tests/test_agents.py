from __future__ import annotations

import json
import subprocess
from pathlib import Path

import backend
import pytest
from agents.codex import (
    CODEX_SUBMIT_SETTLE_MAX_SECONDS,
    CODEX_SUBMIT_SETTLE_MIN_SECONDS,
    CodexAgent,
    codex_submit_settle_seconds,
)


class FakeBridge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    def type(self, session: str, text: str) -> None:
        self.calls.append(("type", session, text))

    def keys(self, session: str, keys: list[str]) -> None:
        self.calls.append(("keys", session, list(keys)))


def test_supported_agents_include_codex_and_claude():
    assert backend.supported_agent_names() == ("claude", "codex")


def test_ensure_managed_claude_home_writes_stop_hook(tmp_path, monkeypatch):
    monkeypatch.setattr(backend.claude_agent_module, "DEFAULT_RUNTIME_HOME_ROOT", tmp_path / "managed")
    monkeypatch.setattr(backend.claude_agent_module, "source_claude_config_path", lambda: tmp_path / ".claude.json")
    monkeypatch.setattr(
        backend.claude_agent_module,
        "source_claude_config_backup_path",
        lambda: tmp_path / ".claude.json.orche.bak",
    )

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
    assert source_payload["projects"][str(tmp_path.resolve())]["hasTrustDialogAccepted"] is True


def test_ensure_managed_claude_home_preserves_existing_source_config(tmp_path, monkeypatch):
    monkeypatch.setattr(backend.claude_agent_module, "DEFAULT_RUNTIME_HOME_ROOT", tmp_path / "managed")
    monkeypatch.setattr(backend.claude_agent_module, "source_claude_config_path", lambda: tmp_path / ".claude.json")
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


def test_ensure_session_supports_claude_agent(xdg_runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(backend.claude_agent_module, "DEFAULT_RUNTIME_HOME_ROOT", tmp_path / "managed")
    monkeypatch.setattr(backend, "ensure_pane", lambda session, cwd, agent, **kwargs: "%7")
    monkeypatch.setattr(backend, "ensure_agent_running", lambda *args, **kwargs: "%7")

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


def test_run_session_watchdog_completes_turn_when_capture_shows_completion(xdg_runtime, tmp_path, monkeypatch):
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

    result = backend.run_session_watchdog(session, turn_id="turn-1", poll_interval=0.01, notify_buffer=0.0)
    meta = backend.load_meta(session)

    assert result == "completed"
    assert emitted and emitted[0]["event"] == "completed"
    assert "pending_turn" not in meta
    assert meta["last_completed_turn"]["summary"] == "OK42"


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


def test_ensure_pane_inline_mode_splits_current_tmux_session(xdg_runtime, tmp_path, monkeypatch):
    tmux_calls = []

    monkeypatch.setattr(backend, "bridge_name_pane", lambda pane_id, session: None)

    def fake_tmux(*args, **kwargs):
        tmux_calls.append(args)
        if list(args) == ["display-message", "-p", "-t", "%1", "#{pane_id}"]:
            return subprocess.CompletedProcess(["tmux", *args], 0, "%1\n", "")
        if args[:2] == ("split-window", "-d"):
            return subprocess.CompletedProcess(
                ["tmux", *args],
                0,
                "orche-reviewer\t%11\t@3\tmain\n",
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
        call[:8] == ("split-window", "-d", "-h", "-p", str(backend.INLINE_PANE_PERCENT), "-t", "%1", "-c")
        for call in tmux_calls
    )


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
