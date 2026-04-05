from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

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
E2E_TIMEOUT = float(os.environ.get("ORCHE_E2E_TIMEOUT", "180"))
CODEX_READY_HINTS = (
    "Approvals:",
    "model:",
    "full-auto",
    "dangerously-bypass-approvals-and-sandbox",
    "Esc to interrupt",
    "Ctrl-C to interrupt",
)
CODEX_LOGIN_PROMPTS = ("Login with ChatGPT", "Please login")
CLAUDE_READY_HINTS = (
    "Claude Code",
    "permission mode",
    "/help",
    "shift+tab",
    "esc to interrupt",
)
CLAUDE_LOGIN_PROMPTS = ("Please run /login", "Login required")


def _require_e2e_environment() -> None:
    if not E2E_ENABLED:
        pytest.skip("set ORCHE_RUN_E2E=1 to run session collaboration e2e tests")
    if shutil.which("tmux") is None:
        pytest.skip("tmux is required for session collaboration e2e tests")
    if shutil.which("codex") is None:
        pytest.skip("codex is required for session collaboration e2e tests")
    if shutil.which("claude") is None:
        pytest.skip("claude is required for session collaboration e2e tests")


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
        result = _run_orche(["read", session, "--lines", "260"], env=env, timeout=30)
        _assert_ok(result)
        last_output = result.stdout
        if all(needle in last_output for needle in needles):
            return last_output
        time.sleep(1.0)
    raise AssertionError(f"timed out waiting for output in {session}: {needles}\nlast output:\n{last_output}")


def _wait_for_codex_ready(env: Dict[str, str], session: str, *, cwd: Path, timeout: float = E2E_TIMEOUT) -> str:
    deadline = time.time() + timeout
    last_output = ""
    while time.time() < deadline:
        result = _run_orche(["read", session, "--lines", "260"], env=env, timeout=30)
        _assert_ok(result)
        last_output = result.stdout
        if any(prompt in last_output for prompt in CODEX_LOGIN_PROMPTS):
            pytest.skip("codex is not logged in for session collaboration e2e tests")
        lowered = last_output.lower()
        has_brand = "openai codex" in lowered or "\ncodex" in lowered or " codex" in lowered
        has_context = str(cwd) in last_output or any(hint.lower() in lowered for hint in CODEX_READY_HINTS)
        if has_brand and has_context:
            return last_output
        time.sleep(1.0)
    raise AssertionError(f"timed out waiting for Codex ready surface in {session}\nlast output:\n{last_output}")


def _wait_for_claude_ready(env: Dict[str, str], session: str, *, cwd: Path, timeout: float = E2E_TIMEOUT) -> str:
    deadline = time.time() + timeout
    last_output = ""
    while time.time() < deadline:
        result = _run_orche(["read", session, "--lines", "260"], env=env, timeout=30)
        _assert_ok(result)
        last_output = result.stdout
        if any(prompt in last_output for prompt in CLAUDE_LOGIN_PROMPTS):
            pytest.skip("claude is not logged in for session collaboration e2e tests")
        lowered = last_output.lower()
        has_brand = "claude code" in lowered or "\nclaude" in lowered or " claude" in lowered
        has_context = str(cwd) in last_output or any(hint in lowered for hint in CLAUDE_READY_HINTS)
        if has_brand and has_context:
            return last_output
        time.sleep(1.0)
    raise AssertionError(f"timed out waiting for Claude ready surface in {session}\nlast output:\n{last_output}")


def _wait_for_agent_ready(agent: str, env: Dict[str, str], session: str, *, cwd: Path, timeout: float = E2E_TIMEOUT) -> str:
    if agent == "claude":
        return _wait_for_claude_ready(env, session, cwd=cwd, timeout=timeout)
    return _wait_for_codex_ready(env, session, cwd=cwd, timeout=timeout)


@dataclass
class E2EContext:
    env: Dict[str, str]
    sessions: List[str] = field(default_factory=list)

    def run(self, args: List[str], *, input_text: str | None = None, timeout: float = E2E_TIMEOUT) -> subprocess.CompletedProcess[str]:
        return _run_orche(args, env=self.env, input_text=input_text, timeout=timeout)

    def create_native_session(self, suffix: str, *, agent: str = "codex") -> str:
        session = f"orche-e2e-reviewer-{agent}-{suffix}-{uuid.uuid4().hex[:8]}"
        result = self.run(
            [
                "open",
                "--cwd",
                str(REPO_ROOT),
                "--agent",
                agent,
                "--name",
                session,
            ]
        )
        _assert_ok(result)
        self.sessions.append(session)
        return session

    def create_managed_session(self, suffix: str, *, agent: str = "codex", notify_target: str) -> str:
        session = f"orche-e2e-worker-{agent}-{suffix}-{uuid.uuid4().hex[:8]}"
        result = self.run(
            [
                "open",
                "--cwd",
                str(REPO_ROOT),
                "--agent",
                agent,
                "--name",
                session,
                "--notify",
                f"tmux:{notify_target}",
            ]
        )
        _assert_ok(result)
        self.sessions.append(session)
        return session

    def close_all(self) -> None:
        for session in reversed(self.sessions):
            self.run(["close", session], timeout=30)


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
    env["TERM"] = env.get("TERM") or "xterm-256color"
    env.pop("ORCHE_SESSION", None)
    env.pop("ORCHE_DISCORD_CHANNEL_ID", None)
    ctx = E2EContext(env=env)
    try:
        yield ctx
    finally:
        ctx.close_all()


@pytest.mark.parametrize(
    ("reviewer_agent", "worker_agent", "token_prefix"),
    [
        ("codex", "codex", "E2E_CODEX_CODEX"),
        ("codex", "claude", "E2E_CODEX_CLAUDE"),
        ("claude", "codex", "E2E_CLAUDE_CODEX"),
        ("claude", "claude", "E2E_CLAUDE_CLAUDE"),
    ],
)
def test_e2e_worker_session_notifies_reviewer_with_real_turn(
    e2e_context: E2EContext,
    reviewer_agent: str,
    worker_agent: str,
    token_prefix: str,
):
    reviewer = e2e_context.create_native_session("reviewer", agent=reviewer_agent)
    worker = e2e_context.create_managed_session("worker", agent=worker_agent, notify_target=reviewer)

    _wait_for_agent_ready(reviewer_agent, e2e_context.env, reviewer, cwd=REPO_ROOT)
    _wait_for_agent_ready(worker_agent, e2e_context.env, worker, cwd=REPO_ROOT)

    token = f"{token_prefix}_{uuid.uuid4().hex[:10].upper()}"
    prompt = (
        "Reply with exactly this single token and nothing else on one line: "
        f"{token}. Do not add punctuation, quotes, bullets, or explanations."
    )
    prompt_result = e2e_context.run(["prompt", worker, prompt])
    _assert_ok(prompt_result)

    source_marker = f"source={worker}" if reviewer_agent == "claude" else f"source session: {worker}"
    reviewer_output = _wait_for_output(
        e2e_context.env,
        reviewer,
        "orche notify",
        source_marker,
        token,
    )
    worker_output = _wait_for_output(
        e2e_context.env,
        worker,
        token,
    )

    if reviewer_agent == "claude":
        assert "event=" in reviewer_output
        assert "status=" in reviewer_output
    else:
        assert "event:" in reviewer_output
        assert "status:" in reviewer_output
    assert token in worker_output
