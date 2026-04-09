from __future__ import annotations

import io
import os
import time
from pathlib import Path

import subprocess
import sys
import re
import pytest

from typer.testing import CliRunner

import backend
import cli
from cli import app

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain_output(result) -> str:
    return ANSI_ESCAPE_RE.sub("", result.output)


def test_utf8_stream_rewraps_ascii_stream():
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="ascii")

    wrapped = cli._utf8_stream(stream)

    assert getattr(wrapped, "encoding", "").lower() == "utf-8"
    wrapped.write("prefix … suffix\n")
    wrapped.flush()
    assert raw.getvalue().decode("utf-8") == "prefix … suffix\n"


def test_backend_run_decodes_utf8_output_under_ascii_locale():
    result = backend.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'pane \\xe2\\x95\\xad\\xe2\\x80\\xa2\\n')",
        ],
        capture=True,
        env={
            **os.environ,
            "LANG": "C",
            "LC_ALL": "C",
            "PYTHONUTF8": "0",
            "PYTHONIOENCODING": "utf-8",
        },
    )

    assert result.stdout == "pane ╭•\n"


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
    original_session_metadata_is_live = backend.session_metadata_is_live
    backend.session_metadata_is_live = lambda session, meta=None: True
    try:
        sessions = backend.list_sessions()
    finally:
        backend.session_metadata_is_live = original_session_metadata_is_live

    assert [entry["session"] for entry in sessions] == ["alpha-session", "zeta-session"]
    assert sessions[0]["cwd"] == "/tmp/alpha"


def test_list_sessions_prunes_stale_metadata(xdg_runtime, monkeypatch):
    backend.save_meta(
        "live-session",
        {
            "session": "live-session",
            "cwd": "/tmp/live",
            "agent": "codex",
            "pane_id": "%1",
        },
    )
    backend.save_meta(
        "stale-session",
        {
            "session": "stale-session",
            "cwd": "/tmp/stale",
            "agent": "codex",
            "pane_id": "%2",
            "tmux_session": backend.tmux_session_name("stale-session"),
        },
    )

    monkeypatch.setattr(backend, "pane_exists", lambda pane_id: pane_id == "%1")
    monkeypatch.setattr(backend, "bridge_resolve", lambda session: None)
    monkeypatch.setattr(backend, "_tmux_has_session", lambda session_name: False)

    sessions = backend.list_sessions()

    assert [entry["session"] for entry in sessions] == ["live-session"]
    assert not backend.meta_path("stale-session").exists()


def test_session_exists_returns_false_for_stale_metadata(xdg_runtime, monkeypatch):
    backend.save_meta(
        "stale-session",
        {
            "session": "stale-session",
            "cwd": "/tmp/stale",
            "agent": "claude",
            "pane_id": "%8",
            "tmux_session": backend.tmux_session_name("stale-session"),
        },
    )

    monkeypatch.setattr(backend, "pane_exists", lambda pane_id: False)
    monkeypatch.setattr(backend, "bridge_resolve", lambda session: None)
    monkeypatch.setattr(backend, "_tmux_has_session", lambda session_name: False)

    assert backend.session_exists("stale-session") is False
    assert not backend.meta_path("stale-session").exists()


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


def test_attach_session_switches_to_dedicated_tmux_session(xdg_runtime, monkeypatch):
    backend.save_meta(
        "demo-session",
        {
            "session": "demo-session",
            "tmux_session": backend.tmux_session_name("demo-session"),
            "pane_id": "%9",
        },
    )
    calls: list[tuple[str, ...]] = []

    def fake_tmux(*args, **kwargs):
        calls.append(tuple(args))
        if list(args) == ["has-session", "-t", backend.tmux_session_name("demo-session")]:
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")
        if list(args[:2]) == ["switch-client", "-t"]:
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")
        return subprocess.CompletedProcess(["tmux", *args], 1, "", "")

    monkeypatch.setenv("TMUX", "1")
    monkeypatch.setattr(backend, "bridge_resolve", lambda session: "")
    monkeypatch.setattr(backend, "tmux", fake_tmux)

    target = backend.attach_session("demo-session")

    assert target == backend.tmux_session_name("demo-session")
    assert ("switch-client", "-t", backend.tmux_session_name("demo-session")) in calls


def test_close_session_kills_dedicated_tmux_session(xdg_runtime, monkeypatch):
    backend.save_meta(
        "demo-session",
        {
            "session": "demo-session",
            "agent": "codex",
            "pane_id": "%9",
            "tmux_session": backend.tmux_session_name("demo-session"),
        },
    )
    calls: list[tuple[str, ...]] = []

    def fake_tmux(*args, **kwargs):
        calls.append(tuple(args))
        if list(args) == ["has-session", "-t", backend.tmux_session_name("demo-session")]:
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")
        if list(args[:4]) == ["list-clients", "-t", backend.tmux_session_name("demo-session"), "-F"]:
            return subprocess.CompletedProcess(["tmux", *args], 0, "/dev/ttys001\n", "")
        if list(args[:2]) == ["detach-client", "-t"]:
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")
        if list(args[:2]) == ["kill-session", "-t"]:
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")
        return subprocess.CompletedProcess(["tmux", *args], 1, "", "")

    monkeypatch.setattr(backend, "bridge_resolve", lambda session: "%9")
    monkeypatch.setattr(
        backend,
        "get_pane_info",
        lambda pane_id: {"session_name": backend.tmux_session_name("demo-session")} if pane_id == "%9" else None,
    )
    monkeypatch.setattr(backend, "pane_exists", lambda pane_id: pane_id == "%9")
    monkeypatch.setattr(backend, "tmux", fake_tmux)

    pane_id = backend.close_session("demo-session")

    assert pane_id == "%9"
    assert ("detach-client", "-t", "/dev/ttys001") in calls
    assert ("kill-session", "-t", backend.tmux_session_name("demo-session")) in calls


def test_list_command_shows_sessions(xdg_runtime, monkeypatch):
    backend.save_meta(
        "demo-session",
        {
            "session": "demo-session",
            "cwd": "/repo/demo",
            "agent": "codex",
            "pane_id": "%1",
        },
    )
    monkeypatch.setattr(backend, "session_metadata_is_live", lambda session, meta=None: True)

    result = CliRunner().invoke(app, ["list"])

    assert result.exit_code == 0
    assert "demo-session" in result.stdout
    assert "/repo/demo" in result.stdout


def test_close_all_closes_every_session(xdg_runtime, monkeypatch):
    runner = CliRunner()
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        cli,
        "list_sessions",
        lambda: [
            {"session": "alpha", "cwd": "/repo/alpha", "agent": "codex"},
            {"session": "beta", "cwd": "/repo/beta", "agent": "claude"},
        ],
    )

    def fake_resolve_session_context(*, session: str, require_existing: bool = False, require_cwd_agent: bool = False):
        return Path(f"/repo/{session}"), "codex" if session == "alpha" else "claude", {"session": session}

    monkeypatch.setattr(cli, "resolve_session_context", fake_resolve_session_context)
    monkeypatch.setattr(cli, "close_session", lambda session: f"%{1 if session == 'alpha' else 2}")
    monkeypatch.setattr(
        cli,
        "append_action_history",
        lambda session, cwd, agent, action, **fields: calls.append((session, str(fields.get("pane_id") or ""))),
    )

    result = runner.invoke(app, ["close", "--all"])

    assert result.exit_code == 0
    assert "alpha" in result.stdout
    assert "beta" in result.stdout
    assert calls == [("alpha", "%1"), ("beta", "%2")]


def test_close_all_rejects_session_argument(xdg_runtime):
    result = CliRunner().invoke(app, ["close", "demo-session", "--all"])

    assert result.exit_code == 1
    assert "close does not accept a session argument with --all" in result.output


def test_ensure_tmux_session_creates_dedicated_session(xdg_runtime, monkeypatch, tmp_path):
    calls: list[tuple[str, ...]] = []
    created = {"value": False}
    expected_tmux_session = backend.tmux_session_name("demo-worker")

    def fake_tmux(*args, **kwargs):
        calls.append(tuple(args))
        if list(args) == ["has-session", "-t", expected_tmux_session]:
            code = 0 if created["value"] else 1
            return subprocess.CompletedProcess(["tmux", *args], code, "", "")
        if list(args[:4]) == ["new-session", "-d", "-s", expected_tmux_session]:
            created["value"] = True
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")
        return subprocess.CompletedProcess(["tmux", *args], 1, "", "")

    monkeypatch.setattr(backend, "tmux", fake_tmux)

    tmux_session = backend.ensure_tmux_session("demo-worker", tmp_path)

    assert tmux_session == expected_tmux_session
    assert ("new-session", "-d", "-s", expected_tmux_session, "-n", "orche-demo-worker", "-c", str(tmp_path)) in calls


def test_list_panes_parses_tmux_separator_output(xdg_runtime, monkeypatch):
    separator = backend.TMUX_PANE_OUTPUT_SEPARATOR

    def fake_tmux(*args, **kwargs):
        if args[:1] == ("list-panes",):
            return subprocess.CompletedProcess(
                ["tmux", *args],
                0,
                (
                    "orche-demo"
                    f"{separator}%7"
                    f"{separator}@1"
                    f"{separator}main"
                    f"{separator}0"
                    f"{separator}123"
                    f"{separator}codex"
                    f"{separator}/tmp/repo"
                    f"{separator}demo\n"
                ),
                "",
            )
        return subprocess.CompletedProcess(["tmux", *args], 1, "", "")

    monkeypatch.setattr(backend, "tmux", fake_tmux)

    panes = backend.list_panes("orche-demo")

    assert panes == [
        {
            "session_name": "orche-demo",
            "pane_id": "%7",
            "window_id": "@1",
            "window_name": "main",
            "pane_dead": "0",
            "pane_pid": "123",
            "pane_current_command": "codex",
            "pane_current_path": "/tmp/repo",
            "pane_title": "demo",
        }
    ]


def test_get_pane_info_parses_tmux_separator_output(xdg_runtime, monkeypatch):
    separator = backend.TMUX_PANE_OUTPUT_SEPARATOR

    monkeypatch.setattr(backend, "pane_exists", lambda pane_id: pane_id == "%7")

    def fake_tmux(*args, **kwargs):
        if list(args) == [
            "display-message",
            "-p",
            "-t",
            "%7",
            backend._tmux_join_fields(
                "#{session_name}",
                "#{pane_id}",
                "#{window_id}",
                "#{window_name}",
                "#{pane_dead}",
                "#{pane_pid}",
                "#{pane_current_command}",
                "#{pane_current_path}",
                "#{pane_title}",
            ),
        ]:
            return subprocess.CompletedProcess(
                ["tmux", *args],
                0,
                (
                    "orche-demo"
                    f"{separator}%7"
                    f"{separator}@1"
                    f"{separator}main"
                    f"{separator}0"
                    f"{separator}123"
                    f"{separator}codex"
                    f"{separator}/tmp/repo"
                    f"{separator}demo\n"
                ),
                "",
            )
        return subprocess.CompletedProcess(["tmux", *args], 1, "", "")

    monkeypatch.setattr(backend, "tmux", fake_tmux)

    info = backend.get_pane_info("%7")

    assert info == {
        "session_name": "orche-demo",
        "pane_id": "%7",
        "window_id": "@1",
        "window_name": "main",
        "pane_dead": "0",
        "pane_pid": "123",
        "pane_current_command": "codex",
        "pane_current_path": "/tmp/repo",
        "pane_title": "demo",
    }


def test_current_session_id_prefers_tmux_session_mapping(xdg_runtime, monkeypatch):
    monkeypatch.delenv("ORCHE_SESSION", raising=False)

    def fake_tmux(*args, **kwargs):
        if list(args) == ["display-message", "-p", "#{session_name}"]:
            return subprocess.CompletedProcess(["tmux"], 0, f"{backend.tmux_session_name('mapped-session')}\n", "")
        return subprocess.CompletedProcess(["tmux"], 1, "", "")

    monkeypatch.setattr(backend, "tmux", fake_tmux)
    monkeypatch.setattr(backend, "list_sessions", lambda: [{"session": "mapped-session", "tmux_session": backend.tmux_session_name("mapped-session")}])

    assert backend.current_session_id() == "mapped-session"


def test_current_session_id_prefers_pane_mapping_over_shared_tmux_session_mapping(xdg_runtime, monkeypatch):
    monkeypatch.delenv("ORCHE_SESSION", raising=False)

    def fake_tmux(*args, **kwargs):
        if list(args) == ["display-message", "-p", "#{pane_id}"]:
            return subprocess.CompletedProcess(["tmux"], 0, "%22\n", "")
        if list(args) == ["display-message", "-p", "#{session_name}"]:
            return subprocess.CompletedProcess(["tmux"], 0, "orche-reviewer\n", "")
        return subprocess.CompletedProcess(["tmux"], 1, "", "")

    monkeypatch.setattr(backend, "tmux", fake_tmux)
    monkeypatch.setattr(
        backend,
        "list_sessions",
        lambda: [
            {"session": "reviewer", "tmux_session": "orche-reviewer", "pane_id": "%21"},
            {"session": "worker", "tmux_session": "orche-reviewer", "pane_id": "%22"},
        ],
    )

    assert backend.current_session_id() == "worker"


def test_attach_session_selects_inline_pane_inside_current_tmux_session(xdg_runtime, monkeypatch):
    backend.save_meta(
        "demo-inline",
        {
            "session": "demo-inline",
            "tmux_session": "orche-reviewer",
            "tmux_mode": "inline-pane",
            "pane_id": "%9",
            "window_id": "@4",
        },
    )
    calls: list[tuple[str, ...]] = []

    def fake_tmux(*args, **kwargs):
        calls.append(tuple(args))
        if list(args) == ["has-session", "-t", "orche-reviewer"]:
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")
        if list(args) == ["display-message", "-p", "#{session_name}"]:
            return subprocess.CompletedProcess(["tmux", *args], 0, "orche-reviewer\n", "")
        if list(args[:2]) in (["select-window", "-t"], ["select-pane", "-t"]):
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")
        return subprocess.CompletedProcess(["tmux", *args], 1, "", "")

    monkeypatch.setenv("TMUX", "1")
    monkeypatch.setattr(backend, "bridge_resolve", lambda session: "%9")
    monkeypatch.setattr(backend, "get_pane_info", lambda pane_id: {"session_name": "orche-reviewer", "window_id": "@4"} if pane_id == "%9" else None)
    monkeypatch.setattr(backend, "tmux", fake_tmux)

    target = backend.attach_session("demo-inline")

    assert target == "orche-reviewer"
    assert ("select-window", "-t", "@4") in calls
    assert ("select-pane", "-t", "%9") in calls
    assert not any(call[:2] == ("switch-client", "-t") for call in calls)


def test_close_session_kills_inline_pane_without_killing_tmux_session(xdg_runtime, monkeypatch):
    backend.save_meta(
        "demo-inline",
        {
            "session": "demo-inline",
            "agent": "codex",
            "pane_id": "%9",
            "tmux_session": "orche-reviewer",
            "tmux_mode": "inline-pane",
        },
    )
    calls: list[tuple[str, ...]] = []

    def fake_tmux(*args, **kwargs):
        calls.append(tuple(args))
        if list(args[:2]) == ["kill-pane", "-t"]:
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")
        return subprocess.CompletedProcess(["tmux", *args], 1, "", "")

    monkeypatch.setattr(backend, "bridge_resolve", lambda session: "%9")
    monkeypatch.setattr(backend, "pane_exists", lambda pane_id: pane_id == "%9")
    monkeypatch.setattr(backend, "get_pane_info", lambda pane_id: {"session_name": "orche-reviewer"} if pane_id == "%9" else None)
    monkeypatch.setattr(backend, "tmux", fake_tmux)

    pane_id = backend.close_session("demo-inline")

    assert pane_id == "%9"
    assert ("kill-pane", "-t", "%9") in calls
    assert not any(call[:2] == ("kill-session", "-t") for call in calls)


def test_close_session_closes_child_sessions_recursively(xdg_runtime, monkeypatch):
    backend.save_meta(
        "parent",
        {
            "session": "parent",
            "agent": "codex",
            "pane_id": "%1",
            "tmux_session": backend.tmux_session_name("parent"),
            "tmux_mode": "dedicated-session",
        },
    )
    backend.save_meta(
        "child",
        {
            "session": "child",
            "agent": "codex",
            "pane_id": "%2",
            "tmux_session": backend.tmux_session_name("parent"),
            "tmux_mode": "inline-pane",
            "parent_session": "parent",
        },
    )
    calls: list[tuple[str, ...]] = []

    def fake_tmux(*args, **kwargs):
        calls.append(tuple(args))
        if list(args) == ["has-session", "-t", backend.tmux_session_name("parent")]:
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")
        if list(args[:4]) == ["list-clients", "-t", backend.tmux_session_name("parent"), "-F"]:
            return subprocess.CompletedProcess(["tmux", *args], 0, "/dev/ttys001\n", "")
        if list(args[:2]) in (["detach-client", "-t"], ["kill-session", "-t"], ["kill-pane", "-t"]):
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")
        return subprocess.CompletedProcess(["tmux", *args], 1, "", "")

    monkeypatch.setattr(backend, "bridge_resolve", lambda session: {"parent": "%1", "child": "%2"}.get(session, ""))
    monkeypatch.setattr(backend, "pane_exists", lambda pane_id: pane_id in {"%1", "%2"})
    monkeypatch.setattr(
        backend,
        "get_pane_info",
        lambda pane_id: {"session_name": backend.tmux_session_name("parent")} if pane_id in {"%1", "%2"} else None,
    )
    monkeypatch.setattr(backend, "tmux", fake_tmux)

    pane_id = backend.close_session("parent")

    assert pane_id == "%1"
    assert ("kill-pane", "-t", "%2") in calls
    assert ("kill-session", "-t", backend.tmux_session_name("parent")) in calls
    assert backend.load_meta("parent") == {}
    assert backend.load_meta("child") == {}


def test_create_inline_pane_reflows_workers_into_grid(xdg_runtime, monkeypatch):
    backend.save_meta(
        "worker-1",
        {
            "session": "worker-1",
            "tmux_mode": "inline-pane",
            "tmux_host_session": "orche-reviewer",
            "host_pane_id": "%host",
            "pane_id": "%left-top",
            "inline_slot": 0,
        },
    )
    backend.save_meta(
        "worker-2",
        {
            "session": "worker-2",
            "tmux_mode": "inline-pane",
            "tmux_host_session": "orche-reviewer",
            "host_pane_id": "%host",
            "pane_id": "%right-top",
            "inline_slot": 1,
        },
    )
    backend.save_meta(
        "worker-3",
        {
            "session": "worker-3",
            "tmux_mode": "inline-pane",
            "tmux_host_session": "orche-reviewer",
            "host_pane_id": "%host",
            "pane_id": "%left-bottom",
            "inline_slot": 2,
        },
    )

    pane_info = {
        "%host": {"session_name": "orche-reviewer", "window_id": "@host", "window_name": "main", "pane_dead": "0"},
        "%left-top": {"session_name": "orche-reviewer", "window_id": "@host", "window_name": "main", "pane_dead": "0"},
        "%right-top": {"session_name": "orche-reviewer", "window_id": "@host", "window_name": "main", "pane_dead": "0"},
        "%left-bottom": {
            "session_name": "orche-reviewer",
            "window_id": "@host",
            "window_name": "main",
            "pane_dead": "0",
        },
        "%right-bottom": {
            "session_name": "orche-reviewer",
            "window_id": "@tmp",
            "window_name": "tmp",
            "pane_dead": "0",
            "pane_id": "%right-bottom",
        },
    }
    calls: list[tuple[str, ...]] = []

    def fake_tmux(*args, **kwargs):
        calls.append(tuple(args))
        if list(args[:2]) == ["new-window", "-d"]:
            stdout = backend._tmux_join_fields("orche-reviewer", "%right-bottom", "@tmp", "tmp") + "\n"
            return subprocess.CompletedProcess(["tmux", *args], 0, stdout, "")
        return subprocess.CompletedProcess(["tmux", *args], 0, "", "")

    monkeypatch.setattr(backend, "session_metadata_is_live", lambda session, meta=None: True)
    monkeypatch.setattr(backend, "pane_exists", lambda pane_id: pane_id in pane_info)
    monkeypatch.setattr(backend, "get_pane_info", lambda pane_id: dict(pane_info[pane_id]) if pane_id in pane_info else None)
    monkeypatch.setattr(backend, "tmux", fake_tmux)

    pane, host = backend.create_inline_pane(
        "worker-4",
        Path("/repo"),
        tmux_session="orche-reviewer",
        host_pane_id="%host",
    )

    assert host == "%host"
    assert pane["pane_id"] == "%right-bottom"
    assert pane["inline_slot"] == "3"
    assert ("break-pane", "-d", "-s", "%left-top") in calls
    assert ("break-pane", "-d", "-s", "%right-top") in calls
    assert ("break-pane", "-d", "-s", "%left-bottom") in calls
    join_calls = [call for call in calls if call and call[0] == "join-pane"]
    assert join_calls == [
        (
            "join-pane",
            "-d",
            "-h",
            "-l",
            "50%",
            "-s",
            "%left-top",
            "-t",
            "%host",
        ),
        (
            "join-pane",
            "-d",
            "-h",
            "-l",
            "50%",
            "-s",
            "%right-top",
            "-t",
            "%left-top",
        ),
        (
            "join-pane",
            "-d",
            "-v",
            "-l",
            "50%",
            "-s",
            "%left-bottom",
            "-t",
            "%left-top",
        ),
        (
            "join-pane",
            "-d",
            "-v",
            "-l",
            "50%",
            "-s",
            "%right-bottom",
            "-t",
            "%right-top",
        ),
    ]


def test_create_inline_pane_rejects_when_inline_limit_is_reached(xdg_runtime, monkeypatch):
    backend.save_config({"max_inline_sessions": 2})
    backend.save_meta(
        "worker-1",
        {
            "session": "worker-1",
            "tmux_mode": "inline-pane",
            "tmux_host_session": "orche-reviewer",
            "host_pane_id": "%host",
            "pane_id": "%pane-1",
            "inline_slot": 0,
        },
    )
    backend.save_meta(
        "worker-2",
        {
            "session": "worker-2",
            "tmux_mode": "inline-pane",
            "tmux_host_session": "orche-reviewer",
            "host_pane_id": "%host",
            "pane_id": "%pane-2",
            "inline_slot": 1,
        },
    )

    calls: list[tuple[str, ...]] = []
    pane_info = {
        "%host": {"session_name": "orche-reviewer", "window_id": "@host", "window_name": "main", "pane_dead": "0"},
        "%pane-1": {"session_name": "orche-reviewer", "window_id": "@host", "window_name": "main", "pane_dead": "0"},
        "%pane-2": {"session_name": "orche-reviewer", "window_id": "@host", "window_name": "main", "pane_dead": "0"},
    }

    def fake_tmux(*args, **kwargs):
        calls.append(tuple(args))
        return subprocess.CompletedProcess(["tmux", *args], 0, "", "")

    monkeypatch.setattr(backend, "session_metadata_is_live", lambda session, meta=None: True)
    monkeypatch.setattr(backend, "pane_exists", lambda pane_id: pane_id in pane_info)
    monkeypatch.setattr(backend, "get_pane_info", lambda pane_id: dict(pane_info[pane_id]) if pane_id in pane_info else None)
    monkeypatch.setattr(backend, "tmux", fake_tmux)

    with pytest.raises(backend.OrcheError, match="Inline pane limit reached"):
        backend.create_inline_pane(
            "worker-3",
            Path("/repo"),
            tmux_session="orche-reviewer",
            host_pane_id="%host",
        )

    assert not any(call[:2] == ("new-window", "-d") for call in calls)


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


def test_config_supports_managed_ttl_seconds(xdg_runtime):
    runner = CliRunner()

    set_result = runner.invoke(app, ["config", "set", "managed.ttl-seconds", "1800"])
    get_result = runner.invoke(app, ["config", "get", "managed.ttl-seconds"])
    list_result = runner.invoke(app, ["config", "list"])

    assert set_result.exit_code == 0
    assert set_result.stdout.strip() == "1800"
    assert get_result.exit_code == 0
    assert get_result.stdout.strip() == "1800"
    assert list_result.exit_code == 0
    assert "managed.ttl-seconds" in list_result.stdout
    assert "1800" in list_result.stdout


def test_config_rejects_non_integer_managed_ttl_seconds(xdg_runtime):
    result = CliRunner().invoke(app, ["config", "set", "managed.ttl-seconds", "soon"])

    assert result.exit_code == 1
    assert "managed.ttl-seconds must be an integer number of seconds" in result.output


def test_config_supports_inline_max_sessions(xdg_runtime):
    runner = CliRunner()

    set_result = runner.invoke(app, ["config", "set", "inline.max-sessions", "3"])
    get_result = runner.invoke(app, ["config", "get", "inline.max-sessions"])
    list_result = runner.invoke(app, ["config", "list"])

    assert set_result.exit_code == 0
    assert set_result.stdout.strip() == "3"
    assert get_result.exit_code == 0
    assert get_result.stdout.strip() == "3"
    assert list_result.exit_code == 0
    assert "inline.max-sessions" in list_result.stdout
    assert "3" in list_result.stdout


def test_config_rejects_out_of_range_inline_max_sessions(xdg_runtime):
    result = CliRunner().invoke(app, ["config", "set", "inline.max-sessions", "5"])

    assert result.exit_code == 1
    assert "inline.max-sessions must be between 1 and 4" in result.output


def test_config_supports_claude_command_home_and_config_paths(xdg_runtime):
    runner = CliRunner()

    set_command = runner.invoke(app, ["config", "set", "claude.command", "/opt/bin/claude-wrapper"])
    set_home_path = runner.invoke(app, ["config", "set", "claude.home-path", "~/custom/.claude"])
    set_config_path = runner.invoke(app, ["config", "set", "claude.config-path", "~/custom/claude.json"])
    get_command = runner.invoke(app, ["config", "get", "claude.command"])
    get_home_path = runner.invoke(app, ["config", "get", "claude.home-path"])
    get_config_path = runner.invoke(app, ["config", "get", "claude.config-path"])
    list_result = runner.invoke(app, ["config", "list"])

    assert set_command.exit_code == 0
    assert set_home_path.exit_code == 0
    assert set_config_path.exit_code == 0
    assert get_command.exit_code == 0
    assert get_home_path.exit_code == 0
    assert get_config_path.exit_code == 0
    assert get_command.stdout.strip() == "/opt/bin/claude-wrapper"
    assert get_home_path.stdout.strip() == "~/custom/.claude"
    assert get_config_path.stdout.strip() == "~/custom/claude.json"
    assert list_result.exit_code == 0
    assert "claude.command" in list_result.stdout
    assert "/opt/bin/claude-wrapper" in list_result.stdout
    assert "claude.home-path" in list_result.stdout
    assert "~/custom/.claude" in list_result.stdout
    assert "claude.config-path" in list_result.stdout
    assert "~/custom/claude.json" in list_result.stdout


def test_config_get_reports_effective_defaults_for_unset_keys(xdg_runtime):
    runner = CliRunner()

    command = runner.invoke(app, ["config", "get", "claude.command"])
    home_path = runner.invoke(app, ["config", "get", "claude.home-path"])
    config_path_result = runner.invoke(app, ["config", "get", "claude.config-path"])
    inline_max = runner.invoke(app, ["config", "get", "inline.max-sessions"])
    notify_enabled = runner.invoke(app, ["config", "get", "notify.enabled"])
    managed_ttl = runner.invoke(app, ["config", "get", "managed.ttl-seconds"])

    assert command.exit_code == 0
    assert command.stdout.strip() == "claude"
    assert home_path.exit_code == 0
    assert home_path.stdout.strip() == "~/.claude"
    assert config_path_result.exit_code == 0
    assert config_path_result.stdout.strip() == "~/.claude.json"
    assert inline_max.exit_code == 0
    assert inline_max.stdout.strip() == str(backend.DEFAULT_MAX_INLINE_SESSIONS)
    assert notify_enabled.exit_code == 0
    assert notify_enabled.stdout.strip() == "true"
    assert managed_ttl.exit_code == 0
    assert managed_ttl.stdout.strip() == str(backend.DEFAULT_MANAGED_SESSION_TTL_SECONDS)


def test_config_set_accepts_multi_token_claude_values(xdg_runtime):
    runner = CliRunner()

    set_command = runner.invoke(
        app,
        ["config", "set", "claude.command", "/opt/tools/happy-coder", "claude", "--happy-starting-mode", "remote"],
    )
    set_home_path = runner.invoke(
        app,
        ["config", "set", "claude.home-path", "/tmp/Claude", "Home/runtime"],
    )
    set_config_path = runner.invoke(
        app,
        ["config", "set", "claude.config-path", "/tmp/Claude", "Config/custom.json"],
    )
    get_command = runner.invoke(app, ["config", "get", "claude.command"])
    get_home_path = runner.invoke(app, ["config", "get", "claude.home-path"])
    get_config_path = runner.invoke(app, ["config", "get", "claude.config-path"])

    assert set_command.exit_code == 0
    assert set_home_path.exit_code == 0
    assert set_config_path.exit_code == 0
    assert get_command.stdout.strip() == "/opt/tools/happy-coder claude --happy-starting-mode remote"
    assert get_home_path.stdout.strip() == "/tmp/Claude Home/runtime"
    assert get_config_path.stdout.strip() == "/tmp/Claude Config/custom.json"


def test_config_reset_restores_default_and_removes_override(xdg_runtime):
    runner = CliRunner()

    assert runner.invoke(app, ["config", "set", "claude.command", "/opt/tools/claude-wrapper"]).exit_code == 0

    reset_result = runner.invoke(app, ["config", "reset", "claude.command"])
    get_result = runner.invoke(app, ["config", "get", "claude.command"])
    list_result = runner.invoke(app, ["config", "list"])
    payload = backend.load_raw_config()

    assert reset_result.exit_code == 0
    assert reset_result.stdout.strip() == "claude"
    assert get_result.exit_code == 0
    assert get_result.stdout.strip() == "claude"
    assert list_result.exit_code == 0
    assert "claude.command" in list_result.stdout
    assert "claude" in list_result.stdout
    assert "claude_command" not in payload


def test_config_reset_preserves_internal_runtime_fields(xdg_runtime):
    backend.save_config(
        {
            "_comment": "runtime",
            "claude_command": "/opt/tools/claude-wrapper",
            "session": "demo-session",
            "runtime_home": "/tmp/runtime-home",
            "tmux_session": "orche-demo-session",
        }
    )

    result = CliRunner().invoke(app, ["config", "reset", "claude.command"])
    payload = backend.load_raw_config()

    assert result.exit_code == 0
    assert result.stdout.strip() == "claude"
    assert "claude_command" not in payload
    assert payload["session"] == "demo-session"
    assert payload["runtime_home"] == "/tmp/runtime-home"
    assert payload["tmux_session"] == "orche-demo-session"


def test_config_reset_supports_non_string_defaults(xdg_runtime):
    runner = CliRunner()

    assert runner.invoke(app, ["config", "set", "notify.enabled", "false"]).exit_code == 0
    assert runner.invoke(app, ["config", "set", "managed.ttl-seconds", "1800"]).exit_code == 0

    notify_result = runner.invoke(app, ["config", "reset", "notify.enabled"])
    ttl_result = runner.invoke(app, ["config", "reset", "managed.ttl-seconds"])

    assert notify_result.exit_code == 0
    assert notify_result.stdout.strip() == "true"
    assert ttl_result.exit_code == 0
    assert ttl_result.stdout.strip() == str(backend.DEFAULT_MANAGED_SESSION_TTL_SECONDS)


def test_config_reset_rejects_unsupported_key(xdg_runtime):
    result = CliRunner().invoke(app, ["config", "reset", "discord.channel-id"])

    assert result.exit_code == 1
    assert "Unsupported config key: discord.channel-id" in result.output


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


def test_build_status_includes_lifecycle_metadata(xdg_runtime, monkeypatch):
    now = time.time()
    backend.save_meta(
        "parent",
        {
            "session": "parent",
            "cwd": "/repo/parent",
            "agent": "codex",
            "pane_id": "%1",
            "launch_mode": "managed",
            "last_event_at": now,
            "expires_after_seconds": 3600,
        },
    )
    backend.save_meta(
        "child",
        {
            "session": "child",
            "cwd": "/repo/child",
            "agent": "codex",
            "pane_id": "%2",
            "launch_mode": "managed",
            "parent_session": "parent",
            "last_event_at": now - 30,
            "expires_after_seconds": 3600,
        },
    )
    monkeypatch.setattr(backend, "bridge_resolve", lambda session: {"parent": "%1", "child": "%2"}.get(session, ""))
    monkeypatch.setattr(backend, "pane_exists", lambda pane_id: pane_id in {"%1", "%2"})
    monkeypatch.setattr(backend, "is_agent_running", lambda plugin, pane_id: pane_id in {"%1", "%2"})
    monkeypatch.setattr(
        backend,
        "get_pane_info",
        lambda pane_id: {"session_name": "orche-parent", "window_name": "main"} if pane_id in {"%1", "%2"} else None,
    )

    status = backend.build_status("child")

    assert status["parent_session"] == "parent"
    assert status["child_count"] == 0
    assert status["ttl_seconds"] == 3600
    assert status["ttl_exempt_because_parent_alive"] is True
    assert status["last_event_at"] == now - 30


def test_expire_managed_sessions_closes_stale_roots_and_preserves_native(xdg_runtime, monkeypatch):
    now = time.time()
    backend.save_config({"_comment": "runtime", "managed_session_ttl_seconds": 60})
    backend.save_meta(
        "managed-stale",
        {
            "session": "managed-stale",
            "agent": "codex",
            "pane_id": "%1",
            "launch_mode": "managed",
            "tmux_session": backend.tmux_session_name("managed-stale"),
            "last_event_at": now - 120,
            "expires_after_seconds": 60,
        },
    )
    backend.save_meta(
        "native-stale",
        {
            "session": "native-stale",
            "agent": "codex",
            "pane_id": "%2",
            "launch_mode": "native",
            "tmux_session": backend.tmux_session_name("native-stale"),
            "last_event_at": now - 120,
            "expires_after_seconds": 60,
        },
    )
    calls: list[tuple[str, ...]] = []

    def fake_tmux(*args, **kwargs):
        calls.append(tuple(args))
        if list(args[:2]) in (["kill-session", "-t"], ["detach-client", "-t"]):
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")
        if list(args[:4]) == ["list-clients", "-t", backend.tmux_session_name("managed-stale"), "-F"]:
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")
        if list(args) == ["has-session", "-t", backend.tmux_session_name("managed-stale")]:
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")
        return subprocess.CompletedProcess(["tmux", *args], 1, "", "")

    monkeypatch.setattr(backend, "bridge_resolve", lambda session: {"managed-stale": "%1", "native-stale": "%2"}.get(session, ""))
    monkeypatch.setattr(backend, "pane_exists", lambda pane_id: pane_id in {"%1", "%2"})
    monkeypatch.setattr(
        backend,
        "get_pane_info",
        lambda pane_id: {"session_name": backend.tmux_session_name("managed-stale")} if pane_id == "%1" else {"session_name": backend.tmux_session_name("native-stale")} if pane_id == "%2" else None,
    )
    monkeypatch.setattr(backend, "tmux", fake_tmux)

    expired = backend.expire_managed_sessions(now=now)

    assert expired == ["managed-stale"]
    assert backend.load_meta("managed-stale") == {}
    assert backend.load_meta("native-stale")["launch_mode"] == "native"
    assert ("kill-session", "-t", backend.tmux_session_name("managed-stale")) in calls


def test_expire_managed_sessions_skips_child_while_parent_alive(xdg_runtime, monkeypatch):
    now = time.time()
    backend.save_config({"_comment": "runtime", "managed_session_ttl_seconds": 60})
    backend.save_meta(
        "parent",
        {
            "session": "parent",
            "agent": "codex",
            "pane_id": "%1",
            "launch_mode": "managed",
            "tmux_session": backend.tmux_session_name("parent"),
            "last_event_at": now,
            "expires_after_seconds": 60,
        },
    )
    backend.save_meta(
        "child",
        {
            "session": "child",
            "agent": "codex",
            "pane_id": "%2",
            "launch_mode": "managed",
            "tmux_mode": "inline-pane",
            "parent_session": "parent",
            "last_event_at": now - 600,
            "expires_after_seconds": 60,
        },
    )

    monkeypatch.setattr(backend, "bridge_resolve", lambda session: {"parent": "%1", "child": "%2"}.get(session, ""))
    monkeypatch.setattr(backend, "pane_exists", lambda pane_id: pane_id in {"%1", "%2"})
    monkeypatch.setattr(
        backend,
        "get_pane_info",
        lambda pane_id: {"session_name": backend.tmux_session_name("parent")} if pane_id in {"%1", "%2"} else None,
    )

    expired = backend.expire_managed_sessions(now=now)

    assert expired == []
    assert backend.load_meta("child")["parent_session"] == "parent"


def test_open_auto_names_inline_child_sessions_from_parent(xdg_runtime, monkeypatch, tmp_path):
    captured: dict[str, str] = {}

    monkeypatch.setattr(cli, "current_session_id", lambda: "parent-main")
    monkeypatch.setattr(cli, "session_exists", lambda session: False)
    def fake_ensure_session(session, cwd, agent, notify_to=None, notify_target=None):
        captured["session"] = session
        return "%9"

    monkeypatch.setattr(cli, "ensure_session", fake_ensure_session)
    monkeypatch.setattr(cli, "append_action_history", lambda *args, **kwargs: None)

    session, pane_id = cli._open_session(
        cwd=tmp_path,
        agent="codex",
        name=None,
        notify="tmux:parent-main",
        cli_args=[],
    )

    assert pane_id == "%9"
    assert session == captured["session"]
    assert session.startswith(f"{backend.repo_name(tmp_path)}-codex-parent-main-")


def test_version_works_without_subcommand(xdg_runtime):
    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip().startswith("orche ")


def test_short_help_works_without_subcommand(xdg_runtime):
    result = CliRunner().invoke(app, ["-h"])

    assert result.exit_code == 0
    assert "Usage: orche" in _plain_output(result)


def test_short_version_works_without_subcommand(xdg_runtime):
    result = CliRunner().invoke(app, ["-v"])

    assert result.exit_code == 0
    assert result.stdout.strip().startswith("orche ")


def test_config_group_supports_short_help(xdg_runtime):
    result = CliRunner().invoke(app, ["config", "-h"])

    assert result.exit_code == 0
    assert "Usage: orche config" in _plain_output(result)


def test_config_group_does_not_accept_short_version(xdg_runtime):
    result = CliRunner().invoke(app, ["config", "-v"])

    assert result.exit_code == 2
    assert "No such option: -v" in _plain_output(result)


def test_leaf_commands_do_not_gain_short_help_aliases(xdg_runtime):
    result = CliRunner().invoke(app, ["attach", "-h"])

    assert result.exit_code == 2
    assert "No such option: -h" in _plain_output(result)


def test_whoami_falls_back_to_backend_resolution(xdg_runtime, monkeypatch):
    monkeypatch.setattr(cli, "current_session_id", lambda: "resolved-from-tmux")

    result = CliRunner().invoke(app, ["whoami"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "resolved-from-tmux"


def test_open_expands_cwd_user_home_for_native_session(xdg_runtime, monkeypatch):
    runner = CliRunner()
    project_dir = xdg_runtime["home"] / "project"
    project_dir.mkdir()
    captured: dict[str, object] = {}

    def fake_ensure_native_session(session, cwd, agent, **kwargs):
        captured["session"] = session
        captured["cwd"] = cwd
        captured["agent"] = agent
        captured["cli_args"] = kwargs.get("cli_args")
        return "%1"

    monkeypatch.setattr(cli, "ensure_native_session", fake_ensure_native_session)
    monkeypatch.setattr(cli.secrets, "token_hex", lambda nbytes: "abc123")
    monkeypatch.setattr(cli, "append_action_history", lambda *args, **kwargs: None)

    result = runner.invoke(
        app,
        [
            "open",
            "--cwd",
            "~/project",
            "--agent",
            "codex",
            "--model",
            "gpt-5.4",
        ],
    )

    assert result.exit_code == 0
    assert captured["session"] == "project-codex-abc123"
    assert captured["cwd"] == project_dir.resolve()
    assert captured["agent"] == "codex"
    assert captured["cli_args"] == ["--model", "gpt-5.4"]
    assert "open ok: session=project-codex-abc123" in result.output


def test_open_passes_notify_binding_to_managed_session(xdg_runtime, monkeypatch):
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
    monkeypatch.setattr(cli.secrets, "token_hex", lambda nbytes: "abc123")
    monkeypatch.setattr(cli, "append_action_history", lambda *args, **kwargs: None)

    result = runner.invoke(
        app,
        [
            "open",
            "--cwd",
            "~/project",
            "--agent",
            "codex",
            "--notify",
            "tmux:target-session",
        ],
    )

    assert result.exit_code == 0
    assert captured["session"] == "project-codex-abc123"
    assert captured["kwargs"]["notify_to"] == "tmux-bridge"
    assert captured["kwargs"]["notify_target"] == "target-session"
    assert "open ok: session=project-codex-abc123" in result.output


def test_open_rejects_existing_session_name(xdg_runtime, monkeypatch):
    runner = CliRunner()
    project_dir = xdg_runtime["home"] / "project"
    project_dir.mkdir()

    monkeypatch.chdir(project_dir)
    monkeypatch.setattr(cli, "session_exists", lambda session: session == "existing-session")

    result = runner.invoke(
        app,
        [
            "open",
            "--agent",
            "codex",
            "--name",
            "existing-session",
            "--model",
            "gpt-5.4",
        ],
    )

    assert result.exit_code == 1
    assert "Session existing-session already exists" in result.output
    assert "orche attach" in result.output


def test_open_rejects_invalid_notify_binding(xdg_runtime):
    runner = CliRunner()
    project_dir = xdg_runtime["home"] / "project"
    project_dir.mkdir()

    result = runner.invoke(
        app,
        [
            "open",
            "--cwd",
            "~/project",
            "--agent",
            "codex",
            "--notify",
            "discord",
        ],
    )

    assert result.exit_code == 1
    assert "--notify must be in the form <provider>:<target>" in result.output


def test_open_rejects_notify_when_raw_agent_args_are_present(xdg_runtime):
    runner = CliRunner()
    project_dir = xdg_runtime["home"] / "project"
    project_dir.mkdir()

    result = runner.invoke(
        app,
        [
            "open",
            "--cwd",
            "~/project",
            "--agent",
            "codex",
            "--notify",
            "discord:1234567890",
            "--model",
            "gpt-5.4",
        ],
    )

    assert result.exit_code == 1
    assert "open does not support combining --notify with raw agent args" in result.output


def test_open_rejects_prompt_when_raw_agent_args_are_present(xdg_runtime):
    runner = CliRunner()
    project_dir = xdg_runtime["home"] / "project"
    project_dir.mkdir()

    result = runner.invoke(
        app,
        [
            "open",
            "--cwd",
            "~/project",
            "--agent",
            "claude",
            "--prompt",
            "review auth changes",
            "--",
            "--print",
            "--help",
        ],
    )

    assert result.exit_code == 1
    assert "open does not support combining --prompt with raw agent args" in result.output


def test_attach_command_uses_session_name_positionally(xdg_runtime, monkeypatch):
    runner = CliRunner()
    recorded: dict[str, object] = {}

    monkeypatch.setattr(cli, "attach_session", lambda session, **kwargs: recorded.update({"session": session, **kwargs}) or "@1")
    monkeypatch.setattr(cli, "_record_session_action", lambda session, action, **kwargs: recorded.update({"action": action}))

    result = runner.invoke(app, ["attach", "demo-session"])

    assert result.exit_code == 0
    assert recorded["session"] == "demo-session"
    assert recorded["action"] == "attach"
    assert "attach ok: session=demo-session target=@1" in result.output


def test_codex_shortcut_opens_native_session_and_attaches(xdg_runtime, monkeypatch):
    runner = CliRunner()
    project_dir = xdg_runtime["home"] / "project"
    project_dir.mkdir()
    captured: dict[str, object] = {}

    monkeypatch.chdir(project_dir)
    monkeypatch.setattr(cli.secrets, "token_hex", lambda nbytes: "abc123")

    def fake_ensure_native_session(session, cwd, agent, **kwargs):
        captured["open"] = {
            "session": session,
            "cwd": cwd,
            "agent": agent,
            "cli_args": kwargs.get("cli_args"),
        }
        return "%9"

    def fake_attach_session(session, **kwargs):
        captured["attach"] = {"session": session, **kwargs}
        return "@1"

    monkeypatch.setattr(cli, "ensure_native_session", fake_ensure_native_session)
    monkeypatch.setattr(cli, "attach_session", fake_attach_session)
    monkeypatch.setattr(cli, "append_action_history", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_record_session_action", lambda session, action, **kwargs: captured.update({"recorded": {"session": session, "action": action, **kwargs}}))

    result = runner.invoke(app, ["codex", "--model", "gpt-5.4"])

    assert result.exit_code == 0
    assert captured["open"] == {
        "session": "project-codex-abc123",
        "cwd": project_dir.resolve(),
        "agent": "codex",
        "cli_args": ["--model", "gpt-5.4"],
    }
    assert captured["attach"] == {"session": "project-codex-abc123", "pane_id": "%9"}
    assert captured["recorded"] == {"session": "project-codex-abc123", "action": "attach"}


def test_codex_shortcut_supports_orche_cwd_and_name_without_forwarding_them(xdg_runtime, monkeypatch):
    runner = CliRunner()
    project_dir = xdg_runtime["home"] / "project"
    project_dir.mkdir()
    captured: dict[str, object] = {}

    monkeypatch.chdir(xdg_runtime["home"])

    def fake_ensure_native_session(session, cwd, agent, **kwargs):
        captured["open"] = {
            "session": session,
            "cwd": cwd,
            "agent": agent,
            "cli_args": kwargs.get("cli_args"),
        }
        return "%9"

    monkeypatch.setattr(cli, "ensure_native_session", fake_ensure_native_session)
    monkeypatch.setattr(cli, "attach_session", lambda session, **kwargs: captured.update({"attach": {"session": session, **kwargs}}) or "@1")
    monkeypatch.setattr(cli, "append_action_history", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_record_session_action", lambda session, action, **kwargs: captured.update({"recorded": {"session": session, "action": action}}))

    result = runner.invoke(
        app,
        ["codex", "--cwd", str(project_dir), "--name", "demo-shortcut", "--model", "gpt-5.4"],
    )

    assert result.exit_code == 0
    assert captured["open"] == {
        "session": "demo-shortcut",
        "cwd": project_dir.resolve(),
        "agent": "codex",
        "cli_args": ["--model", "gpt-5.4"],
    }
    assert captured["attach"] == {"session": "demo-shortcut", "pane_id": "%9"}
    assert captured["recorded"] == {"session": "demo-shortcut", "action": "attach"}


def test_claude_shortcut_generates_unique_sessions_and_forwards_raw_args(xdg_runtime, monkeypatch):
    runner = CliRunner()
    project_dir = xdg_runtime["home"] / "project"
    project_dir.mkdir()
    token_values = iter(["abc123", "def456"])
    sessions: list[str] = []
    cli_args: list[list[str]] = []

    monkeypatch.chdir(project_dir)
    monkeypatch.setattr(cli.secrets, "token_hex", lambda nbytes: next(token_values))
    monkeypatch.setattr(
        cli,
        "ensure_native_session",
        lambda session, cwd, agent, **kwargs: sessions.append(session) or cli_args.append(list(kwargs.get("cli_args") or [])) or "%3",
    )
    monkeypatch.setattr(cli, "attach_session", lambda *args, **kwargs: "@1")
    monkeypatch.setattr(cli, "append_action_history", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_record_session_action", lambda *args, **kwargs: None)

    first = runner.invoke(app, ["claude", "--", "--print", "--help"])
    second = runner.invoke(app, ["claude"])

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert sessions == ["project-claude-abc123", "project-claude-def456"]
    assert cli_args == [["--print", "--help"], []]


def test_prompt_command_uses_positionals(xdg_runtime, monkeypatch):
    runner = CliRunner()
    project_dir = xdg_runtime["home"] / "project"
    project_dir.mkdir()

    monkeypatch.setattr(
        cli,
        "resolve_session_context",
        lambda **kwargs: (project_dir.resolve(), "codex", {"session": "demo-session"}),
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "send_prompt", lambda session, cwd, agent, message: captured.update({"session": session, "cwd": cwd, "agent": agent, "message": message}) or "%1")

    result = runner.invoke(app, ["prompt", "demo-session", "review auth changes"])

    assert result.exit_code == 0
    assert captured == {
        "session": "demo-session",
        "cwd": project_dir.resolve(),
        "agent": "codex",
        "message": "review auth changes",
    }
    assert "prompt ok: session=demo-session" in result.output


def test_open_command_supports_initial_prompt(xdg_runtime, monkeypatch):
    runner = CliRunner()
    project_dir = xdg_runtime["home"] / "project"
    project_dir.mkdir()
    captured: dict[str, object] = {}

    monkeypatch.setattr(cli, "_open_session", lambda **kwargs: ("demo-session", "%1"))
    monkeypatch.setattr(
        cli,
        "send_prompt",
        lambda session, cwd, agent, message: captured.update(
            {"session": session, "cwd": cwd, "agent": agent, "message": message}
        )
        or "%1",
    )

    result = runner.invoke(
        app,
        [
            "open",
            "--cwd",
            "~/project",
            "--agent",
            "claude",
            "--prompt",
            "review auth changes",
        ],
    )

    assert result.exit_code == 0
    assert captured == {
        "session": "demo-session",
        "cwd": project_dir.resolve(),
        "agent": "claude",
        "message": "review auth changes",
    }
    assert "open ok: session=demo-session" in result.output


def test_input_and_key_commands_use_positionals(xdg_runtime, monkeypatch):
    runner = CliRunner()
    project_dir = xdg_runtime["home"] / "project"
    project_dir.mkdir()
    monkeypatch.setattr(
        cli,
        "resolve_session_context",
        lambda **kwargs: (project_dir.resolve(), "codex", {"session": "demo-session"}),
    )
    input_calls: list[tuple[str, str]] = []
    key_calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(cli, "bridge_type", lambda session, text: input_calls.append((session, text)))
    monkeypatch.setattr(cli, "bridge_keys", lambda session, keys: key_calls.append((session, list(keys))))
    monkeypatch.setattr(cli, "append_action_history", lambda *args, **kwargs: None)

    input_result = runner.invoke(app, ["input", "demo-session", "yes"])
    key_result = runner.invoke(app, ["key", "demo-session", "Down", "Enter"])

    assert input_result.exit_code == 0
    assert key_result.exit_code == 0
    assert input_calls == [("demo-session", "yes")]
    assert key_calls == [("demo-session", ["Down", "Enter"])]
    assert "input ok: session=demo-session chars=3" in input_result.output
    assert "key ok: session=demo-session keys=Down,Enter" in key_result.output


def test_tmux_bridge_type_uses_tmux_buffer_for_long_text(xdg_runtime, monkeypatch):
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    monkeypatch.setattr(backend, "_resolve_bridge_pane", lambda session: "%7")

    def fake_tmux(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(["tmux", *args], 0, "", "")

    monkeypatch.setattr(backend, "tmux", fake_tmux)

    result = backend.tmux_bridge("type", "demo-session", "x" * 20000, check=True, capture=True)

    assert result.returncode == 0
    assert [call[0][0] for call in calls] == ["load-buffer", "paste-buffer", "delete-buffer"]
    assert calls[0][0][:3] == ("load-buffer", "-b", calls[0][0][2])
    assert calls[0][1]["input_text"] == "x" * 20000
    assert calls[1][0][:5] == ("paste-buffer", "-t", "%7", "-b", calls[0][0][2])


def test_cancel_command_prints_machine_readable_success(xdg_runtime, monkeypatch):
    runner = CliRunner()
    project_dir = xdg_runtime["home"] / "project"
    project_dir.mkdir()
    monkeypatch.setattr(
        cli,
        "resolve_session_context",
        lambda **kwargs: (project_dir.resolve(), "codex", {"session": "demo-session"}),
    )
    monkeypatch.setattr(cli, "cancel_session", lambda session: "%7")
    monkeypatch.setattr(cli, "append_action_history", lambda *args, **kwargs: None)

    result = runner.invoke(app, ["cancel", "demo-session"])

    assert result.exit_code == 0
    assert "cancel ok: session=demo-session pane=%7" in result.output


def test_close_command_prints_machine_readable_success(xdg_runtime, monkeypatch):
    runner = CliRunner()
    project_dir = xdg_runtime["home"] / "project"
    project_dir.mkdir()
    monkeypatch.setattr(
        cli,
        "resolve_session_context",
        lambda **kwargs: (project_dir.resolve(), "codex", {"session": "demo-session"}),
    )
    monkeypatch.setattr(cli, "close_session", lambda session: "%8")
    monkeypatch.setattr(cli, "append_action_history", lambda *args, **kwargs: None)

    result = runner.invoke(app, ["close", "demo-session"])

    assert result.exit_code == 0
    assert "close ok: session=demo-session pane=%8" in result.output


def test_key_help_explains_sequence_usage():
    result = CliRunner().invoke(app, ["key", "--help"])

    assert result.exit_code == 0
    assert "Send one or more tmux key names to a session in order" in result.output
    assert "Enter" in result.output
    assert "C-c" in result.output
    assert "orche input" in result.output


def test_unknown_command_shows_clean_error(xdg_runtime, capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["orche", "invalidcmd"])
    exit_code = cli.main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Error: Unknown command: invalidcmd" in captured.err
