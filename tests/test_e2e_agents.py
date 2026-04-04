from __future__ import annotations

import subprocess
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from typer.testing import CliRunner

import backend
import cli
from agents.claude import ClaudeAgent
from cli import app


@dataclass
class FakePane:
    pane_id: str
    window_id: str
    window_name: str
    cwd: str
    pane_pid: str
    pane_current_command: str = "zsh"
    pane_dead: str = "0"
    pane_title: str = ""
    capture: str = ""
    descendants: list[str] = field(default_factory=list)
    pending_input: str = ""
    awaiting_approval: bool = False
    agent: str = ""
    launch_pending_enter: bool = False


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.1)


class FakeOrcheRuntime:
    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd.resolve()
        self.tmux_session_exists = False
        self.window_seq = 0
        self.pane_seq = 0
        self.pid_seq = 1000
        self.windows: dict[str, dict[str, str]] = {}
        self.panes: dict[str, FakePane] = {}
        self.session_aliases: dict[str, str] = {}
        self.launch_commands: list[str] = []
        self.prompt_scenarios: dict[str, deque[str]] = {}
        self.force_startup_prompt: set[str] = set()
        self.selected_window_id = ""
        self.attached_session = ""
        self.switched_session = ""
        self.watchdog_starts: list[tuple[str, str]] = []

    def queue_prompt(self, session: str, scenario: str) -> None:
        self.prompt_scenarios.setdefault(session, deque()).append(scenario)

    def require_startup_approval(self, session: str) -> None:
        self.force_startup_prompt.add(session)

    def _result(self, args: list[str], returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)

    def _create_window(self, name: str, cwd: str) -> dict[str, str]:
        self.window_seq += 1
        self.pane_seq += 1
        self.pid_seq += 1
        window_id = f"@{self.window_seq}"
        pane_id = f"%{self.pane_seq}"
        window = {"window_id": window_id, "window_name": name}
        pane = FakePane(
            pane_id=pane_id,
            window_id=window_id,
            window_name=name,
            cwd=cwd,
            pane_pid=str(self.pid_seq),
        )
        self.windows[window_id] = window
        self.panes[pane_id] = pane
        return window

    def _pane_for_target(self, target: str) -> FakePane | None:
        if target in self.panes:
            return self.panes[target]
        for pane in self.panes.values():
            if pane.window_id == target:
                return pane
        return None

    def _ready_capture(self, agent: str, cwd: str) -> str:
        if agent == "claude":
            return f"Claude Code\ncwd: {cwd}\n/help"
        return f"OpenAI Codex\ncwd: {cwd}\nApprovals:"

    def _approval_capture(self, kind: str) -> str:
        if kind == "shell":
            return "Claude Code\nApproval required to run shell command.\nApprove? [y/n]"
        return "Claude Code\nApproval required to edit files.\nApprove? [y/n]"

    def _handle_launch(self, pane: FakePane, launch_command: str) -> None:
        self.launch_commands.append(launch_command)
        pane.pending_input = ""
        pane.awaiting_approval = False
        pane.launch_pending_enter = True
        if "exec claude" in launch_command:
            pane.agent = "claude"
            pane.pane_current_command = "claude"
            pane.descendants = ["claude"]
            session = self._session_from_launch(launch_command)
            skip_permissions = "--dangerously-skip-permissions" in launch_command or "--permission-mode bypassPermissions" in launch_command
            managed_launch = "--settings" in launch_command or "ORCHE_SESSION=" in launch_command
            if session in self.force_startup_prompt or (managed_launch and not skip_permissions):
                pane.capture = self._approval_capture("edit")
                pane.awaiting_approval = True
            else:
                pane.capture = self._ready_capture("claude", pane.cwd)
            return
        pane.agent = "codex"
        pane.pane_current_command = "codex"
        pane.descendants = ["codex"]
        pane.capture = self._ready_capture("codex", pane.cwd)

    def _session_from_launch(self, launch_command: str) -> str:
        marker = "export ORCHE_SESSION="
        if marker not in launch_command:
            return ""
        suffix = launch_command.split(marker, 1)[1].strip()
        token = suffix.split(" && ", 1)[0].strip()
        return token.strip("'\"")

    def _submit_prompt(self, pane: FakePane, session: str) -> None:
        prompt = pane.pending_input.strip()
        pane.pending_input = ""
        scenario = self.prompt_scenarios.get(session, deque())
        next_scenario = scenario.popleft() if scenario else "success"
        if next_scenario == "approval_prompt":
            pane.capture = self._approval_capture("edit")
            pane.awaiting_approval = True
            return
        if next_scenario == "shell_approval_prompt":
            pane.capture = self._approval_capture("shell")
            pane.awaiting_approval = True
            return
        banner = "Claude Code" if pane.agent == "claude" else "OpenAI Codex"
        pane.capture = f"{banner}\nCompleted: {prompt}"
        pane.awaiting_approval = False

    def _handle_bridge_keys(self, pane: FakePane, session: str, values: list[str]) -> None:
        for value in values:
            if value == "Enter":
                if pane.launch_pending_enter:
                    pane.launch_pending_enter = False
                    continue
                if pane.awaiting_approval:
                    reply = pane.pending_input.strip().lower()
                    pane.pending_input = ""
                    pane.awaiting_approval = False
                    if reply in {"y", "yes", "approve", "allow"}:
                        pane.capture = "Claude Code\nApproval accepted\nCompleted after approval"
                    else:
                        pane.capture = "Claude Code\nApproval declined"
                else:
                    self._submit_prompt(pane, session)
            elif value == "C-c":
                pane.pending_input = ""
                pane.awaiting_approval = False
                banner = "Claude Code" if pane.agent == "claude" else "OpenAI Codex"
                pane.capture = f"{banner}\n^C\nInterrupted"

    def process_descendants(self, root_pid: int) -> list[str]:
        for pane in self.panes.values():
            if int(pane.pane_pid) == root_pid:
                return list(pane.descendants)
        return []

    def tmux(self, *args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
        argv = ["tmux", *args]
        command = args[0]
        if command == "has-session":
            return self._result(argv, 0 if self.tmux_session_exists else 1)
        if command == "new-session":
            self.tmux_session_exists = True
            name = args[args.index("-n") + 1]
            cwd = args[args.index("-c") + 1]
            self._create_window(name, cwd)
            return self._result(argv)
        if command == "new-window":
            name = args[args.index("-n") + 1]
            cwd = args[args.index("-c") + 1]
            self._create_window(name, cwd)
            return self._result(argv)
        if command == "list-windows":
            if not self.tmux_session_exists:
                return self._result(argv, 1)
            stdout = "\n".join(f"{window['window_id']}\t{window['window_name']}" for window in self.windows.values())
            return self._result(argv, stdout=stdout)
        if command == "list-panes":
            target = args[args.index("-t") + 1]
            panes = []
            for pane in self.panes.values():
                if target == backend.TMUX_SESSION or pane.window_id == target or pane.pane_id == target:
                    panes.append(
                        "\t".join(
                            [
                                pane.pane_id,
                                pane.window_id,
                                pane.window_name,
                                pane.pane_dead,
                                pane.pane_pid,
                                pane.pane_current_command,
                                pane.cwd,
                                pane.pane_title,
                            ]
                        )
                    )
            return self._result(argv, stdout="\n".join(panes))
        if command == "display-message":
            pane = self.panes.get(args[args.index("-t") + 1])
            if pane is None:
                return self._result(argv, 1)
            return self._result(argv, stdout=pane.pane_id)
        if command == "select-window":
            self.selected_window_id = args[args.index("-t") + 1]
            return self._result(argv)
        if command == "attach-session":
            self.attached_session = args[args.index("-t") + 1]
            return self._result(argv)
        if command == "switch-client":
            self.switched_session = args[args.index("-t") + 1]
            return self._result(argv)
        if command == "capture-pane":
            pane = self.panes.get(args[args.index("-t") + 1])
            if pane is None:
                return self._result(argv, 1)
            return self._result(argv, stdout=pane.capture)
        if command == "select-pane":
            pane = self.panes[args[args.index("-t") + 1]]
            pane.pane_title = args[args.index("-T") + 1]
            return self._result(argv)
        if command == "respawn-pane":
            pane = self.panes[args[args.index("-t") + 1]]
            pane.pane_dead = "0"
            pane.cwd = args[args.index("-c") + 1]
            pane.pane_current_command = "zsh"
            pane.capture = ""
            pane.descendants = []
            return self._result(argv)
        if command == "send-keys":
            pane = self.panes[args[args.index("-t") + 1]]
            if "-l" in args:
                literal_text = args[args.index("-l") + 1]
                if pane.agent:
                    pane.pending_input += literal_text
                else:
                    self._handle_launch(pane, literal_text)
            else:
                values = [arg for arg in args[args.index("-t") + 2 :] if arg not in {"-t"}]
                self._handle_bridge_keys(pane, pane.pane_title, values)
            return self._result(argv)
        if command == "split-window":
            return self._result(argv)
        if command == "kill-window":
            window_id = args[args.index("-t") + 1]
            pane_ids = [pane_id for pane_id, pane in self.panes.items() if pane.window_id == window_id]
            for pane_id in pane_ids:
                del self.panes[pane_id]
            self.windows.pop(window_id, None)
            aliases_to_remove = [name for name, pane_id in self.session_aliases.items() if pane_id in pane_ids]
            for name in aliases_to_remove:
                del self.session_aliases[name]
            self.tmux_session_exists = bool(self.windows)
            return self._result(argv)
        raise AssertionError(f"Unhandled tmux command: {args}")

    def tmux_bridge(self, *args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
        argv = ["tmux-bridge", *args]
        command = args[0]
        if command == "name":
            pane_id, session = args[1], args[2]
            self.session_aliases[session] = pane_id
            self.panes[pane_id].pane_title = session
            return self._result(argv)
        if command == "resolve":
            session = args[1]
            pane_id = self.session_aliases.get(session, "")
            return self._result(argv, 0 if pane_id else 1, stdout=pane_id)
        if command == "read":
            session = args[1]
            pane = self.panes[self.session_aliases[session]]
            return self._result(argv, stdout=pane.capture)
        if command == "type":
            session, text = args[1], args[2]
            pane = self.panes[self.session_aliases[session]]
            pane.pending_input += text
            return self._result(argv)
        if command == "keys":
            session = args[1]
            pane = self.panes[self.session_aliases[session]]
            self._handle_bridge_keys(pane, session, list(args[2:]))
            return self._result(argv)
        raise AssertionError(f"Unhandled tmux-bridge command: {args}")


def make_runtime(monkeypatch, tmp_path: Path) -> FakeOrcheRuntime:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    source_home = tmp_path / "source-codex"
    source_home.mkdir()
    (source_home / "hooks").mkdir()
    (source_home / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")
    managed_root = tmp_path / "managed"
    managed_root.mkdir()

    runtime = FakeOrcheRuntime(project_dir)
    clock = FakeClock()

    monkeypatch.setattr(backend, "tmux", runtime.tmux)
    monkeypatch.setattr(backend, "process_descendants", runtime.process_descendants)
    monkeypatch.setattr(backend, "require_tmux", lambda: None)
    monkeypatch.setattr(backend.time, "time", clock.time)
    monkeypatch.setattr(backend.time, "sleep", clock.sleep)
    monkeypatch.setattr(backend, "DEFAULT_CODEX_SOURCE_HOME", source_home)
    monkeypatch.setattr(backend, "DEFAULT_CODEX_HOME_ROOT", managed_root)
    monkeypatch.setattr(backend.codex_agent_module, "DEFAULT_CODEX_SOURCE_HOME", source_home)
    monkeypatch.setattr(backend.codex_agent_module, "DEFAULT_RUNTIME_HOME_ROOT", managed_root)
    monkeypatch.setattr(backend.claude_agent_module, "DEFAULT_RUNTIME_HOME_ROOT", managed_root)
    monkeypatch.setattr(
        backend,
        "start_session_watchdog",
        lambda session, *, turn_id="": runtime.watchdog_starts.append((session, turn_id)) or 4321,
    )
    return runtime


def test_codex_e2e_session_lifecycle(xdg_runtime, tmp_path, monkeypatch):
    runtime = make_runtime(monkeypatch, tmp_path)
    runner = CliRunner()
    session = "repo-codex-main"

    result = runner.invoke(
        app,
        [
            "open",
            "--cwd",
            str(runtime.cwd),
            "--agent",
            "codex",
            "--name",
            session,
            "--notify",
            "discord:1234567890",
        ],
    )
    assert result.exit_code == 0
    assert session in result.stdout
    assert "export ORCHE_BIN=" in runtime.launch_commands[-1]
    assert "export PATH=" in runtime.launch_commands[-1]
    assert "codex --no-alt-screen" in runtime.launch_commands[-1]
    assert "--dangerously-bypass-approvals-and-sandbox" in runtime.launch_commands[-1]

    send_result = runner.invoke(app, ["prompt", session, "summarize repo"])
    status_result = runner.invoke(app, ["status", session])
    read_result = runner.invoke(app, ["read", session, "--lines", "40"])
    close_result = runner.invoke(app, ["close", session])

    assert send_result.exit_code == 0
    assert runtime.watchdog_starts
    assert status_result.exit_code == 0
    assert "Agent:" in status_result.stdout
    assert "codex" in status_result.stdout
    assert "Running:" in status_result.stdout
    assert "yes" in status_result.stdout
    assert "Completed: summarize repo" in read_result.stdout
    assert close_result.exit_code == 0
    assert backend.load_meta(session) == {}


def test_claude_e2e_launches_headlessly_without_startup_approval(xdg_runtime, tmp_path, monkeypatch):
    runtime = make_runtime(monkeypatch, tmp_path)
    runner = CliRunner()
    session = "repo-claude-main"

    result = runner.invoke(
        app,
        [
            "open",
            "--cwd",
            str(runtime.cwd),
            "--agent",
            "claude",
            "--name",
            session,
            "--notify",
            "discord:1234567890",
        ],
    )
    read_result = runner.invoke(app, ["read", session, "--lines", "40"])

    assert result.exit_code == 0
    assert "export ORCHE_BIN=" in runtime.launch_commands[-1]
    assert "export PATH=" in runtime.launch_commands[-1]
    assert "claude --dangerously-skip-permissions" in runtime.launch_commands[-1]
    assert "--settings" in runtime.launch_commands[-1]
    assert "Approval required" not in read_result.stdout
    assert "Claude Code" in read_result.stdout


def test_claude_e2e_interactive_approval_prompt_can_be_answered(xdg_runtime, tmp_path, monkeypatch):
    runtime = make_runtime(monkeypatch, tmp_path)
    runner = CliRunner()
    session = "repo-claude-main"
    runtime.queue_prompt(session, "approval_prompt")

    assert runner.invoke(
        app,
        [
            "open",
            "--cwd",
            str(runtime.cwd),
            "--agent",
            "claude",
            "--name",
            session,
            "--notify",
            "discord:1234567890",
        ],
    ).exit_code == 0

    send_result = runner.invoke(app, ["prompt", session, "edit the failing test"])
    read_result = runner.invoke(app, ["read", session, "--lines", "50"])
    status_result = runner.invoke(app, ["status", session])
    type_result = runner.invoke(app, ["input", session, "yes"])
    keys_result = runner.invoke(app, ["key", session, "Enter"])
    final_read_result = runner.invoke(app, ["read", session, "--lines", "50"])

    assert send_result.exit_code == 0
    assert runtime.watchdog_starts[-1][0] == session
    assert "Approval required to edit files" in read_result.stdout
    assert status_result.exit_code == 0
    assert "claude" in status_result.stdout
    assert "yes" in status_result.stdout
    assert type_result.exit_code == 0
    assert keys_result.exit_code == 0
    assert "Approval accepted" in final_read_result.stdout
    assert "Completed after approval" in final_read_result.stdout


def test_claude_e2e_cancel_recovers_from_blocking_prompt_and_keeps_session(xdg_runtime, tmp_path, monkeypatch):
    runtime = make_runtime(monkeypatch, tmp_path)
    runner = CliRunner()
    session = "repo-claude-main"
    runtime.queue_prompt(session, "shell_approval_prompt")

    assert runner.invoke(
        app,
        [
            "open",
            "--cwd",
            str(runtime.cwd),
            "--agent",
            "claude",
            "--name",
            session,
            "--notify",
            "discord:1234567890",
        ],
    ).exit_code == 0
    assert runner.invoke(app, ["prompt", session, "run the formatter"]).exit_code == 0

    blocked_read = runner.invoke(app, ["read", session, "--lines", "40"])
    cancel_result = runner.invoke(app, ["cancel", session])
    after_cancel = runner.invoke(app, ["read", session, "--lines", "40"])
    resend_result = runner.invoke(app, ["prompt", session, "summarize current status"])
    final_read = runner.invoke(app, ["read", session, "--lines", "40"])

    assert "Approval required to run shell command" in blocked_read.stdout
    assert cancel_result.exit_code == 0
    assert "Sent Ctrl-C" in cancel_result.stdout
    assert "Interrupted" in after_cancel.stdout
    assert resend_result.exit_code == 0
    assert "Completed: summarize current status" in final_read.stdout


def test_status_reports_pending_turn_watchdog_state(xdg_runtime, tmp_path, monkeypatch):
    runtime = make_runtime(monkeypatch, tmp_path)
    runner = CliRunner()
    session = "repo-codex-main"

    assert runner.invoke(
        app,
        [
            "open",
            "--cwd",
            str(runtime.cwd),
            "--agent",
            "codex",
            "--name",
            session,
            "--notify",
            "discord:1234567890",
        ],
    ).exit_code == 0

    send_result = runner.invoke(app, ["prompt", session, "analyze the repo"])
    status_result = runner.invoke(app, ["status", session])

    assert send_result.exit_code == 0
    assert status_result.exit_code == 0
    assert "Pending turn:" in status_result.stdout
    assert "Watchdog:" in status_result.stdout


def test_claude_e2e_startup_times_out_if_permission_bypass_is_removed(xdg_runtime, tmp_path, monkeypatch):
    runtime = make_runtime(monkeypatch, tmp_path)
    runner = CliRunner()
    session = "repo-claude-main"
    original_build_launch_command = ClaudeAgent.build_launch_command

    def build_without_bypass(self, **kwargs):
        command = original_build_launch_command(self, **kwargs)
        return command.replace(" --dangerously-skip-permissions", "")

    monkeypatch.setattr(ClaudeAgent, "build_launch_command", build_without_bypass)
    runtime.require_startup_approval(session)

    result = runner.invoke(
        app,
        [
            "open",
            "--cwd",
            str(runtime.cwd),
            "--agent",
            "claude",
            "--name",
            session,
            "--notify",
            "discord:1234567890",
        ],
    )

    assert result.exit_code == 1
    assert "Timed out waiting for Claude Code to become ready" in result.output


def test_open_native_session_can_attach_interactively(xdg_runtime, tmp_path, monkeypatch):
    runtime = make_runtime(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(app, ["open", "--cwd", str(runtime.cwd), "--agent", "codex", "--model", "gpt-5.4"])
    attach_result = runner.invoke(app, ["attach", "project-codex-main"])

    assert result.exit_code == 0
    assert attach_result.exit_code == 0
    assert runtime.attached_session == backend.TMUX_SESSION or runtime.switched_session == backend.TMUX_SESSION
    assert runtime.selected_window_id == "@1"
    assert "export ORCHE_BIN=" in runtime.launch_commands[-1]
    assert "export PATH=" in runtime.launch_commands[-1]
    assert "exec codex --model gpt-5.4" in runtime.launch_commands[-1]
    assert "--dangerously-bypass-approvals-and-sandbox" not in runtime.launch_commands[-1]
    assert "CODEX_HOME" not in runtime.launch_commands[-1]


def test_open_native_session_defaults_to_current_directory(xdg_runtime, tmp_path, monkeypatch):
    runtime = make_runtime(monkeypatch, tmp_path)
    runner = CliRunner()
    monkeypatch.chdir(runtime.cwd)

    result = runner.invoke(app, ["open", "--agent", "claude", "--", "--print", "--help"])

    assert result.exit_code == 0
    assert "export ORCHE_BIN=" in runtime.launch_commands[-1]
    assert "export PATH=" in runtime.launch_commands[-1]
    assert "exec claude --print --help" in runtime.launch_commands[-1]
    assert "--dangerously-skip-permissions" not in runtime.launch_commands[-1]
    assert "--settings" not in runtime.launch_commands[-1]


def test_codex_shortcut_launches_native_session_and_attaches(xdg_runtime, tmp_path, monkeypatch):
    runtime = make_runtime(monkeypatch, tmp_path)
    runner = CliRunner()
    monkeypatch.chdir(runtime.cwd)
    monkeypatch.setattr(cli.secrets, "token_hex", lambda nbytes: "abc123")

    result = runner.invoke(app, ["codex", "--model", "gpt-5.4"])

    assert result.exit_code == 0
    assert runtime.attached_session == backend.TMUX_SESSION or runtime.switched_session == backend.TMUX_SESSION
    assert runtime.selected_window_id == "@1"
    assert backend.load_meta("project-codex-abc123")["pane_id"] == "%1"
    assert "exec codex --model gpt-5.4" in runtime.launch_commands[-1]


def test_attach_falls_back_to_attach_session_when_switch_client_has_no_current_client(xdg_runtime, tmp_path, monkeypatch):
    runtime = make_runtime(monkeypatch, tmp_path)
    runner = CliRunner()
    monkeypatch.setenv("TMUX", "fake-client")

    open_result = runner.invoke(app, ["open", "--cwd", str(runtime.cwd), "--agent", "codex", "--model", "gpt-5.4"])
    assert open_result.exit_code == 0

    def tmux_with_failed_switch(*args: str, **kwargs):
        if list(args) == ["switch-client", "-t", backend.TMUX_SESSION]:
            return subprocess.CompletedProcess(["tmux", *args], 1, stdout="", stderr="no current client")
        return runtime.tmux(*args, **kwargs)

    monkeypatch.setattr(backend, "tmux", tmux_with_failed_switch)

    result = runner.invoke(app, ["attach", "project-codex-main"])

    assert result.exit_code == 0
    assert runtime.attached_session == backend.TMUX_SESSION
