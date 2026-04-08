from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import backend


def _stub_managed_startup_ready(monkeypatch) -> None:
    monkeypatch.setattr(
        backend,
        "wait_for_managed_startup_ready",
        lambda session, plugin, pane_id, cwd, timeout=backend.STARTUP_TIMEOUT: pane_id,
    )


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
    monkeypatch.setattr(backend, "ensure_pane", lambda session, cwd, agent, **kwargs: "%1")
    monkeypatch.setattr(backend, "ensure_agent_running", lambda *args, **kwargs: "%1")
    _stub_managed_startup_ready(monkeypatch)

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
    monkeypatch.setattr(backend, "ensure_pane", lambda session, cwd, agent, **kwargs: "%1")
    monkeypatch.setattr(backend, "ensure_agent_running", lambda *args, **kwargs: "%1")
    _stub_managed_startup_ready(monkeypatch)

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
    monkeypatch.setattr(backend, "ensure_pane", lambda session, cwd, agent, **kwargs: "%1")
    monkeypatch.setattr(backend, "ensure_agent_running", lambda *args, **kwargs: "%1")
    _stub_managed_startup_ready(monkeypatch)

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
    monkeypatch.setattr(backend, "ensure_pane", lambda session, cwd, agent, **kwargs: "%1")
    monkeypatch.setattr(backend, "ensure_agent_running", lambda *args, **kwargs: "%1")
    _stub_managed_startup_ready(monkeypatch)

    with pytest.raises(backend.OrcheError, match="managed sessions require both notify_to and notify_target"):
        backend.ensure_session("notify-bound", tmp_path, "codex")


def test_ensure_session_stores_tmux_bridge_notify_binding(xdg_runtime, tmp_path, monkeypatch):
    source_home = tmp_path / "source-codex"
    source_home.mkdir()
    (source_home / "hooks").mkdir()
    (source_home / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")

    monkeypatch.setattr(backend, "DEFAULT_CODEX_SOURCE_HOME", source_home)
    monkeypatch.setattr(backend, "DEFAULT_CODEX_HOME_ROOT", tmp_path / "managed")
    monkeypatch.setattr(backend, "ensure_pane", lambda session, cwd, agent, **kwargs: "%1")
    monkeypatch.setattr(backend, "ensure_agent_running", lambda *args, **kwargs: "%1")
    _stub_managed_startup_ready(monkeypatch)

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


def test_ensure_agent_running_does_not_reference_inline_locals_when_absent(xdg_runtime, tmp_path, monkeypatch):
    plugin = backend.get_agent("codex")
    backend.save_meta(
        "managed-launch",
        {
            "session": "managed-launch",
            "cwd": str(tmp_path),
            "agent": "codex",
            "pane_id": "%5",
        },
    )

    monkeypatch.setattr(backend, "is_agent_running", lambda plugin, pane_id: False)
    monkeypatch.setattr(backend, "get_pane_info", lambda pane_id: {"pane_dead": "0"} if pane_id == "%5" else None)
    monkeypatch.setattr(backend, "wait_for_agent_process_start", lambda plugin, pane_id: pane_id)
    monkeypatch.setattr(backend, "bridge_name_pane", lambda pane_id, session: None)
    monkeypatch.setattr(plugin, "build_launch_command", lambda **kwargs: "exec codex")
    monkeypatch.setattr(
        backend,
        "tmux",
        lambda *args, **kwargs: subprocess.CompletedProcess(["tmux", *args], 0, "", ""),
    )

    pane_id = backend.ensure_agent_running(
        plugin,
        "managed-launch",
        tmp_path,
        "%5",
        runtime=backend.AgentRuntime(label=plugin.runtime_label),
    )

    meta = backend.load_meta("managed-launch")

    assert pane_id == "%5"
    assert meta["pane_id"] == "%5"
    assert meta["session"] == "managed-launch"


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
