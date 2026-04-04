from __future__ import annotations

import json
from pathlib import Path

import backend
import pytest


def test_supported_agents_include_codex_and_claude():
    assert backend.supported_agent_names() == ("claude", "codex")


def test_ensure_managed_claude_home_writes_stop_hook(tmp_path, monkeypatch):
    monkeypatch.setattr(backend.claude_agent_module, "DEFAULT_RUNTIME_HOME_ROOT", tmp_path / "managed")

    target = backend.ensure_managed_claude_home(
        "repo-claude-main",
        cwd=tmp_path,
        discord_channel_id="1234567890",
    )

    settings_path = Path(target) / "settings.json"
    hook_path = Path(target) / "hooks" / "discord-turn-notify.sh"
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    command = payload["hooks"]["Stop"][0]["hooks"][0]["command"]

    assert hook_path.exists()
    assert "--session repo-claude-main" in command
    assert "--channel-id 1234567890" in command


def test_ensure_session_supports_claude_agent(xdg_runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(backend.claude_agent_module, "DEFAULT_RUNTIME_HOME_ROOT", tmp_path / "managed")
    monkeypatch.setattr(backend, "ensure_pane", lambda session, cwd, agent: "%7")
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
    assert "exec codex --model gpt-5.4" in command


def test_wait_for_agent_process_start_surfaces_explicit_launch_error(monkeypatch):
    plugin = backend.get_agent("codex")
    capture = "orche launch error: Codex CLI not found in PATH. Install codex or add it to PATH."

    monkeypatch.setattr(backend, "read_pane", lambda pane_id, lines=backend.DEFAULT_CAPTURE_LINES: capture)
    monkeypatch.setattr(backend, "get_pane_info", lambda pane_id: {"pane_dead": "0"})
    monkeypatch.setattr(backend, "is_agent_running", lambda plugin, pane_id: False)

    with pytest.raises(backend.OrcheError, match="Codex CLI not found in PATH"):
        backend.wait_for_agent_process_start(plugin, "%1", timeout=0.1)
