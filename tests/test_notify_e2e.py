from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
CLI_RUNNER = (
    "import sys; "
    f"sys.path.insert(0, {str(SRC_DIR)!r}); "
    "import cli; "
    "sys.argv = ['orche', *sys.argv[1:]]; "
    "raise SystemExit(cli.main())"
)
E2E_ENABLED = os.environ.get("ORCHE_RUN_E2E") == "1"
E2E_TIMEOUT = float(os.environ.get("ORCHE_E2E_TIMEOUT", "120"))


def _require_e2e_environment() -> None:
    if not E2E_ENABLED:
        pytest.skip("set ORCHE_RUN_E2E=1 to run notify e2e tests")
    if shutil.which("tmux") is None:
        pytest.skip("tmux is required for notify e2e tests")
    if shutil.which("codex") is None:
        pytest.skip("codex is required for notify e2e tests")


def _run_orche(args: List[str], *, env: Dict[str, str], input_text: str | None = None, timeout: float = E2E_TIMEOUT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", CLI_RUNNER, *args],
        cwd=str(REPO_ROOT),
        env=env,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _assert_ok(result: subprocess.CompletedProcess[str]) -> None:
    if result.returncode != 0:
        raise AssertionError(f"command failed: stdout={result.stdout}\nstderr={result.stderr}")


def _wait_for_output(env: Dict[str, str], session: str, *needles: str, timeout: float = E2E_TIMEOUT) -> str:
    deadline = time.time() + timeout
    last_output = ""
    while time.time() < deadline:
        result = _run_orche(["read", session, "--lines", "240"], env=env, timeout=30)
        _assert_ok(result)
        last_output = result.stdout
        if all(needle in last_output for needle in needles):
            return last_output
        time.sleep(1.0)
    raise AssertionError(f"timed out waiting for output in {session}: {needles}\nlast output:\n{last_output}")


class _WebhookHandler(BaseHTTPRequestHandler):
    server: "_CaptureServer"

    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length).decode("utf-8", errors="replace")
        self.server.requests.append(
            {
                "path": self.path,
                "headers": dict(self.headers.items()),
                "body": body,
            }
        )
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover
        _ = (format, args)


class _CaptureServer(ThreadingHTTPServer):
    def __init__(self, server_address):
        super().__init__(server_address, _WebhookHandler)
        self.requests: List[Dict[str, Any]] = []


@dataclass
class E2EContext:
    env: Dict[str, str]
    sessions: List[str] = field(default_factory=list)

    def run(self, args: List[str], *, input_text: str | None = None, timeout: float = E2E_TIMEOUT) -> subprocess.CompletedProcess[str]:
        return _run_orche(args, env=self.env, input_text=input_text, timeout=timeout)

    def create_session(
        self,
        suffix: str,
        *,
        notify_to: str = "discord",
        notify_target: str = "1111111111",
    ) -> str:
        session = f"orche-e2e-{suffix}-{uuid.uuid4().hex[:8]}"
        args = [
            "open",
            "--cwd",
            str(REPO_ROOT),
            "--agent",
            "codex",
            "--name",
            session,
        ]
        provider = "tmux" if notify_to == "tmux-bridge" else notify_to
        args.extend(["--notify", f"{provider}:{notify_target}"])
        result = self.run(args, timeout=E2E_TIMEOUT)
        _assert_ok(result)
        self.sessions.append(session)
        return session

    def close_all(self) -> None:
        for session in reversed(self.sessions):
            self.run(["close", session], timeout=30)

    def notify(self, session: str, summary: str, *, channel_id: str = "", verbose: bool = False) -> subprocess.CompletedProcess[str]:
        args = ["notify-internal", "--session", session]
        if channel_id:
            args.extend(["--channel-id", channel_id])
        if verbose:
            args.append("--verbose")
        payload = json.dumps({"event": "turn-complete", "summary": summary})
        return self.run(args, input_text=payload)


@pytest.fixture
def e2e_context(tmp_path):
    _require_e2e_environment()
    config_home = tmp_path / "config"
    data_home = tmp_path / "data"
    config_home.mkdir()
    data_home.mkdir()
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = str(config_home)
    env["XDG_DATA_HOME"] = str(data_home)
    env.pop("ORCHE_SESSION", None)
    env.pop("ORCHE_DISCORD_CHANNEL_ID", None)
    ctx = E2EContext(env=env)
    try:
        yield ctx
    finally:
        ctx.close_all()


@pytest.fixture
def webhook_server():
    server = _CaptureServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _write_config(env: Dict[str, str], payload: Dict[str, Any]) -> None:
    config_path = Path(env["XDG_CONFIG_HOME"]) / "orche" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def test_e2e_session_a_notifies_session_b_via_tmux_bridge(e2e_context: E2EContext):
    session_b = e2e_context.create_session("target-b")
    session_a = e2e_context.create_session("source-a", notify_to="tmux-bridge", notify_target=session_b)
    _write_config(
        e2e_context.env,
        {
            "notify_enabled": True,
            "notify_provider": "discord",
        },
    )

    result = e2e_context.notify(session_a, "E2E-TMUX-BRIDGE-ONE")
    _assert_ok(result)

    output = _wait_for_output(
        e2e_context.env,
        session_b,
        "orche notify",
        f"source session: {session_a}",
        "event: completed",
        "E2E-TMUX-BRIDGE-ONE",
    )
    assert "status: success" in output


def test_e2e_concurrent_tmux_bridge_notifications_serialize_same_target(e2e_context: E2EContext):
    target_b = e2e_context.create_session("target-b")
    source_a = e2e_context.create_session("source-a", notify_to="tmux-bridge", notify_target=target_b)
    source_c = e2e_context.create_session("source-c", notify_to="tmux-bridge", notify_target=target_b)
    _write_config(
        e2e_context.env,
        {
            "notify_enabled": True,
            "notify_provider": "discord",
        },
    )

    first_marker = "E2E-CONCURRENT-FIRST"
    second_marker = "E2E-CONCURRENT-SECOND"
    results: List[subprocess.CompletedProcess[str]] = []

    def send(session: str, marker: str) -> None:
        results.append(e2e_context.notify(session, marker))

    first = threading.Thread(target=send, args=(source_a, first_marker))
    second = threading.Thread(target=send, args=(source_c, second_marker))
    first.start()
    second.start()
    first.join()
    second.join()

    for result in results:
        _assert_ok(result)

    output = _wait_for_output(
        e2e_context.env,
        target_b,
        f"source session: {source_a}",
        f"source session: {source_c}",
        first_marker,
        second_marker,
    )
    first_block = (
        f"orche notify\nsource session: {source_a}\nevent: completed\ncwd: {REPO_ROOT}\n\n{first_marker}\n\nstatus: success"
    )
    second_block = (
        f"orche notify\nsource session: {source_c}\nevent: completed\ncwd: {REPO_ROOT}\n\n{second_marker}\n\nstatus: success"
    )
    assert first_block in output
    assert second_block in output


def test_e2e_route_priority_prefers_explicit_then_session_then_global(e2e_context: E2EContext, webhook_server: _CaptureServer):
    session_global = e2e_context.create_session("source-global")
    session_a = e2e_context.create_session(
        "source-session",
        notify_to="discord",
        notify_target="2222222222",
    )
    webhook_url = f"http://127.0.0.1:{webhook_server.server_port}/discord"
    _write_config(
        e2e_context.env,
        {
            "notify_enabled": True,
            "notify_provider": "discord",
            "discord_webhook_url": webhook_url,
            "discord_channel_id": "1111111111",
            "notify_mention_user_id": "",
        },
    )
    global_notify = e2e_context.notify(session_global, "E2E-PRIORITY-GLOBAL", verbose=True)
    _assert_ok(global_notify)
    assert "discord: 1111111111" in global_notify.stdout

    session_notify = e2e_context.notify(session_a, "E2E-PRIORITY-SESSION", verbose=True)
    _assert_ok(session_notify)
    assert "discord: 2222222222" in session_notify.stdout

    explicit_notify = e2e_context.notify(
        session_a,
        "E2E-PRIORITY-EXPLICIT",
        channel_id="3333333333",
        verbose=True,
    )
    _assert_ok(explicit_notify)
    assert "discord: 3333333333" in explicit_notify.stdout

    assert len(webhook_server.requests) == 3
    assert "E2E-PRIORITY-GLOBAL" in json.loads(webhook_server.requests[0]["body"])["content"]
    assert "E2E-PRIORITY-SESSION" in json.loads(webhook_server.requests[1]["body"])["content"]
    assert "E2E-PRIORITY-EXPLICIT" in json.loads(webhook_server.requests[2]["body"])["content"]


def test_e2e_session_binding_overrides_global_provider_without_fanout(e2e_context: E2EContext, webhook_server: _CaptureServer):
    session_b = e2e_context.create_session("target-b")
    session_a = e2e_context.create_session(
        "source-a",
        notify_to="tmux-bridge",
        notify_target=session_b,
    )
    webhook_url = f"http://127.0.0.1:{webhook_server.server_port}/discord"
    _write_config(
        e2e_context.env,
        {
            "notify_enabled": True,
            "notify_provider": "discord",
            "discord_channel_id": "4444444444",
            "discord_webhook_url": webhook_url,
            "notify_mention_user_id": "",
        },
    )

    result = e2e_context.notify(session_a, "E2E-SINGLE-CHANNEL")
    _assert_ok(result)

    output = _wait_for_output(
        e2e_context.env,
        session_b,
        f"source session: {session_a}",
        "E2E-SINGLE-CHANNEL",
    )
    assert "notify ok: provider=tmux-bridge detail=delivered" in result.stdout
    assert "provider=discord" not in result.stdout
    assert "E2E-SINGLE-CHANNEL" in output
    assert len(webhook_server.requests) == 0
