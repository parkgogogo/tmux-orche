from __future__ import annotations

from pathlib import Path

import subprocess
import sys

from typer.testing import CliRunner

import backend
import cli
from cli import app


def test_history_command_shows_recent_entries(xdg_runtime):
    backend.append_history_entry(
        "demo-session",
        {
            "timestamp": "2026-04-01T12:34:56",
            "action": "send",
            "session": "demo-session",
            "prompt": "echo hello",
        },
    )
    backend.append_history_entry(
        "demo-session",
        {
            "timestamp": "2026-04-01T12:35:10",
            "action": "cancel",
            "session": "demo-session",
        },
    )

    result = CliRunner().invoke(app, ["history", "--session", "demo-session", "--limit", "20"])

    assert result.exit_code == 0
    assert "2026-04-01T12:34:56" in result.stdout
    assert "send" in result.stdout
    assert 'prompt: "echo hello"' in result.stdout
    assert "2026-04-01T12:35:10" in result.stdout
    assert "cancel" in result.stdout


def test_history_command_shows_empty_message(xdg_runtime):
    result = CliRunner().invoke(app, ["history", "--session", "missing-session"])

    assert result.exit_code == 0
    assert "No history yet" in result.stdout


def test_backend_list_sessions_returns_sorted_metadata(xdg_runtime):
    backend.save_meta(
        "zeta-session",
        {
            "session": "zeta-session",
            "cwd": "/tmp/zeta",
            "agent": "codex",
            "pane_id": "%2",
        },
    )
    backend.save_meta(
        "alpha-session",
        {
            "session": "alpha-session",
            "cwd": "/tmp/alpha",
            "agent": "codex",
            "pane_id": "%1",
        },
    )

    sessions = backend.list_sessions()

    assert [entry["session"] for entry in sessions] == ["alpha-session", "zeta-session"]
    assert sessions[0]["cwd"] == "/tmp/alpha"


def test_current_session_id_prefers_orche_session_env(xdg_runtime, monkeypatch):
    monkeypatch.setenv("ORCHE_SESSION", "env-session")
    monkeypatch.setattr(backend, "tmux", lambda *args, **kwargs: subprocess.CompletedProcess(["tmux"], 1, "", ""))

    assert backend.current_session_id() == "env-session"


def test_current_session_id_falls_back_to_tmux_pane_mapping(xdg_runtime, monkeypatch):
    monkeypatch.delenv("ORCHE_SESSION", raising=False)

    def fake_tmux(*args, **kwargs):
        if list(args) == ["display-message", "-p", "#{pane_id}"]:
            return subprocess.CompletedProcess(["tmux"], 0, "%7\n", "")
        return subprocess.CompletedProcess(["tmux"], 1, "", "")

    monkeypatch.setattr(backend, "tmux", fake_tmux)
    monkeypatch.setattr(backend, "list_sessions", lambda: [{"session": "mapped-session", "pane_id": "%7"}])

    assert backend.current_session_id() == "mapped-session"


def test_sessions_list_command_shows_sessions(xdg_runtime):
    backend.save_meta(
        "demo-session",
        {
            "session": "demo-session",
            "cwd": "/repo/demo",
            "agent": "codex",
            "pane_id": "%1",
        },
    )

    result = CliRunner().invoke(app, ["sessions", "list"])

    assert result.exit_code == 0
    assert "demo-session" in result.stdout
    assert "/repo/demo" in result.stdout


def test_sessions_clearall_command_closes_all_sessions(xdg_runtime, monkeypatch):
    runner = CliRunner()
    closed = []

    monkeypatch.setattr(
        cli,
        "list_sessions",
        lambda: [
            {"session": "alpha-session"},
            {"session": "beta-session"},
        ],
    )
    monkeypatch.setattr(cli, "close_session", lambda session: closed.append(session) or "-")

    result = runner.invoke(app, ["sessions", "clearall"])

    assert result.exit_code == 0
    assert closed == ["alpha-session", "beta-session"]
    assert "Cleared 2 session(s)" in result.stdout


def test_config_supports_discord_mention_user_id(xdg_runtime):
    runner = CliRunner()

    set_result = runner.invoke(app, ["config", "set", "discord.mention-user-id", "123456"])
    get_result = runner.invoke(app, ["config", "get", "discord.mention-user-id"])
    list_result = runner.invoke(app, ["config", "list"])

    assert set_result.exit_code == 0
    assert "123456" in set_result.stdout
    assert get_result.exit_code == 0
    assert get_result.stdout.strip() == "123456"
    assert list_result.exit_code == 0
    assert "discord.mention-user-id" in list_result.stdout
    assert "123456" in list_result.stdout


def test_config_rejects_discord_channel_id_shortcut(xdg_runtime):
    runner = CliRunner()

    set_result = runner.invoke(app, ["config", "set", "discord.channel-id", "123456"])
    get_result = runner.invoke(app, ["config", "get", "discord.channel-id"])
    list_result = runner.invoke(app, ["config", "list"])

    assert set_result.exit_code == 1
    assert "Unsupported config key: discord.channel-id" in set_result.output
    assert get_result.exit_code == 1
    assert "Unsupported config key: discord.channel-id" in get_result.output
    assert list_result.exit_code == 0
    assert "discord.channel-id" not in list_result.output


def test_build_status_uses_session_metadata_discord_session(xdg_runtime, monkeypatch):
    backend.save_meta(
        "demo-session",
        {
            "session": "demo-session",
            "cwd": "/repo/demo",
            "agent": "codex",
            "pane_id": "%1",
            "notify_binding": {
                "provider": "discord",
                "target": "1111111111",
                "session": "agent:main:discord:channel:1111111111",
            },
        },
    )
    backend.save_config(
        {
            "_comment": "runtime",
            "discord_channel_id": "2222222222",
            "discord_session": "agent:main:discord:channel:2222222222",
        }
    )
    monkeypatch.setattr(backend, "bridge_resolve", lambda session: "")

    status = backend.build_status("demo-session")

    assert status["discord_session"] == "agent:main:discord:channel:1111111111"


def test_version_works_without_subcommand(xdg_runtime):
    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip().startswith("orche ")


def test_session_id_reads_orche_session_env(xdg_runtime, monkeypatch):
    monkeypatch.setenv("ORCHE_SESSION", "demo-session")

    result = CliRunner().invoke(app, ["session-id"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "demo-session"


def test_whoami_falls_back_to_backend_resolution(xdg_runtime, monkeypatch):
    monkeypatch.setattr(cli, "current_session_id", lambda: "resolved-from-tmux")

    result = CliRunner().invoke(app, ["whoami"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "resolved-from-tmux"


def test_session_new_expands_cwd_user_home(xdg_runtime, monkeypatch):
    runner = CliRunner()
    project_dir = xdg_runtime["home"] / "project"
    project_dir.mkdir()
    captured: dict[str, Path] = {}

    def fake_ensure_session(session, cwd, agent, **kwargs):
        captured["session"] = session
        captured["cwd"] = cwd
        captured["agent"] = agent
        return "%1"

    monkeypatch.setattr(cli, "ensure_session", fake_ensure_session)
    monkeypatch.setattr(cli, "append_action_history", lambda *args, **kwargs: None)

    result = runner.invoke(
        app,
        [
            "session-new",
            "--cwd",
            "~/project",
            "--agent",
            "codex",
            "--notify-to",
            "discord",
            "--notify-target",
            "1234567890",
        ],
    )

    assert result.exit_code == 0
    assert captured["cwd"] == project_dir.resolve()
    assert captured["agent"] == "codex"


def test_session_new_passes_notify_binding(xdg_runtime, monkeypatch):
    runner = CliRunner()
    project_dir = xdg_runtime["home"] / "project"
    project_dir.mkdir()
    captured: dict[str, object] = {}

    def fake_ensure_session(session, cwd, agent, **kwargs):
        captured["session"] = session
        captured["cwd"] = cwd
        captured["agent"] = agent
        captured["kwargs"] = kwargs
        return "%1"

    monkeypatch.setattr(cli, "ensure_session", fake_ensure_session)
    monkeypatch.setattr(cli, "append_action_history", lambda *args, **kwargs: None)

    result = runner.invoke(
        app,
        [
            "session-new",
            "--cwd",
            "~/project",
            "--agent",
            "codex",
            "--notify-to",
            "tmux-bridge",
            "--notify-target",
            "target-session",
        ],
    )

    assert result.exit_code == 0
    assert captured["kwargs"]["notify_to"] == "tmux-bridge"
    assert captured["kwargs"]["notify_target"] == "target-session"


def test_session_new_rejects_partial_notify_binding(xdg_runtime):
    runner = CliRunner()
    project_dir = xdg_runtime["home"] / "project"
    project_dir.mkdir()

    result = runner.invoke(
        app,
        [
            "session-new",
            "--cwd",
            "~/project",
            "--agent",
            "codex",
            "--notify-to",
            "discord",
        ],
    )

    assert result.exit_code == 1
    assert "session-new requires both --notify-to and --notify-target" in result.output


def test_session_new_requires_notify_binding(xdg_runtime):
    runner = CliRunner()
    project_dir = xdg_runtime["home"] / "project"
    project_dir.mkdir()

    result = runner.invoke(
        app,
        [
            "session-new",
            "--cwd",
            "~/project",
            "--agent",
            "codex",
        ],
    )

    assert result.exit_code == 1
    assert "session-new requires both --notify-to and --notify-target" in result.output


def test_codex_command_passes_native_args_and_attaches(xdg_runtime, monkeypatch):
    runner = CliRunner()
    project_dir = xdg_runtime["home"] / "project"
    project_dir.mkdir()
    captured: dict[str, object] = {}
    attached: dict[str, object] = {}

    def fake_ensure_native_session(session, cwd, agent, **kwargs):
        captured["session"] = session
        captured["cwd"] = cwd
        captured["agent"] = agent
        captured["cli_args"] = kwargs.get("cli_args")
        return "%1"

    monkeypatch.setattr(cli, "ensure_native_session", fake_ensure_native_session)
    monkeypatch.setattr(cli, "append_action_history", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "attach_session", lambda session, **kwargs: attached.update({"session": session, **kwargs}) or "@1")

    result = runner.invoke(
        app,
        [
            "codex",
            "--cwd",
            "~/project",
            "--session-name",
            "custom-codex",
            "--model",
            "gpt-5.4",
            "--approval-mode",
            "on-request",
        ],
    )

    assert result.exit_code == 0
    assert captured["cwd"] == project_dir.resolve()
    assert captured["agent"] == "codex"
    assert captured["session"] == "custom-codex"
    assert captured["cli_args"] == ["--model", "gpt-5.4", "--approval-mode", "on-request"]
    assert attached["session"] == "custom-codex"
    assert attached["pane_id"] == "%1"


def test_codex_command_defaults_to_current_directory_and_attaches(xdg_runtime, monkeypatch):
    runner = CliRunner()
    project_dir = xdg_runtime["home"] / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    captured: dict[str, object] = {}
    attached: dict[str, object] = {}

    def fake_ensure_native_session(session, cwd, agent, **kwargs):
        captured["session"] = session
        captured["cwd"] = cwd
        captured["agent"] = agent
        return "%7"

    monkeypatch.setattr(cli, "ensure_native_session", fake_ensure_native_session)
    monkeypatch.setattr(cli, "append_action_history", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "attach_session", lambda session, **kwargs: attached.update({"session": session, **kwargs}) or "@7")

    result = runner.invoke(app, ["codex"])

    assert result.exit_code == 0
    assert captured["cwd"] == project_dir.resolve()
    assert captured["agent"] == "codex"
    assert captured["session"] == "project-codex-main"
    assert attached["session"] == "project-codex-main"
    assert attached["pane_id"] == "%7"


def test_claude_aliases_use_claude_agent(xdg_runtime, monkeypatch):
    runner = CliRunner()
    project_dir = xdg_runtime["home"] / "project"
    project_dir.mkdir()
    calls: list[tuple[str, str]] = []
    attached: list[str] = []

    def fake_ensure_native_session(session, cwd, agent, **kwargs):
        calls.append((session, agent))
        return "%1"

    monkeypatch.setattr(cli, "ensure_native_session", fake_ensure_native_session)
    monkeypatch.setattr(cli, "append_action_history", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "attach_session", lambda session, **kwargs: attached.append(session) or "@1")

    for command_name in ("claude", "cc"):
        result = runner.invoke(app, [command_name, "--cwd", "~/project"])
        assert result.exit_code == 0

    assert calls == [
        ("project-claude-main", "claude"),
        ("project-claude-main", "claude"),
    ]
    assert attached == [
        "project-claude-main",
        "project-claude-main",
    ]


def test_cc_help_is_passed_through_to_native_cli(xdg_runtime, monkeypatch):
    runner = CliRunner()
    project_dir = xdg_runtime["home"] / "project"
    project_dir.mkdir()
    captured: dict[str, object] = {}

    def fake_ensure_native_session(session, cwd, agent, **kwargs):
        captured["session"] = session
        captured["cwd"] = cwd
        captured["agent"] = agent
        captured["cli_args"] = kwargs.get("cli_args")
        return "%9"

    monkeypatch.setattr(cli, "ensure_native_session", fake_ensure_native_session)
    monkeypatch.setattr(cli, "append_action_history", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "attach_session", lambda *args, **kwargs: "@9")

    result = runner.invoke(app, ["cc", "--cwd", "~/project", "--help"])

    assert result.exit_code == 0
    assert captured["agent"] == "claude"
    assert captured["cli_args"] == ["--help"]


def test_unknown_command_shows_clean_error(xdg_runtime, capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["orche", "invalidcmd"])
    exit_code = cli.main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Error: Unknown command: invalidcmd" in captured.err
