from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union

import agents.claude as claude_agent_module
import agents.codex as codex_agent_module
from agents import AgentPlugin, AgentRuntime, get_agent_plugin, supported_agents
from agents.claude import ClaudeAgent, default_claude_home_path
from agents.codex import CodexAgent, SOURCE_CONFIG_BACKUP_SUFFIX, default_codex_home_path
from agents.common import (
    ensure_orche_shim,
    normalize_runtime_home,
    orche_bootstrap_command,
    remove_runtime_home,
    validate_discord_channel_id as common_validate_discord_channel_id,
    write_text_atomically,
)
from json_utils import JSONInputTooLargeError, MAX_JSON_INPUT_BYTES, loads_json, read_json_file
from paths import config_path, ensure_directories, history_dir, locks_dir, meta_dir, orch_log_path

BACKEND = "tmux"
TMUX_SESSION = "orche"
LEGACY_TMUX_SESSION = "orche-smux"
DEFAULT_CAPTURE_LINES = 200
INLINE_PANE_PERCENT = 25
DEFAULT_MAX_INLINE_SESSIONS = 4
STARTUP_TIMEOUT = 90.0
CLAUDE_STARTUP_GRACE_SECONDS = 2.0
CLAUDE_PROMPT_ACK_TIMEOUT = 15.0
CLAUDE_PROMPT_ACK_POLL_INTERVAL = 0.1
WATCHDOG_CAPTURE_LINES = DEFAULT_CAPTURE_LINES
NOTIFY_TAIL_LINES = 20
WATCHDOG_POLL_INTERVAL = 3.0
WATCHDOG_STALLED_AFTER = 45.0
WATCHDOG_NEEDS_INPUT_AFTER = 120.0
WATCHDOG_REMINDER_AFTER = 600.0
WATCHDOG_ACTIVE_CPU_THRESHOLD = 5.0
LATEST_TURN_SUMMARY_RETRY_SECONDS = 5.0
LATEST_TURN_SUMMARY_RETRY_INTERVAL = 0.25
WATCHDOG_NOTIFY_BUFFER = 10.0
TMUX_PANE_OUTPUT_SEPARATOR = "@@ORCHE_PANE@@"
LAUNCH_ERROR_PREFIX = "orche launch error:"
DEFAULT_MANAGED_SESSION_TTL_SECONDS = 43200
CONFIG_COMMENT = (
    "orche runtime config. session is the active orche agent session label; "
    "discord_session is the Discord/OpenClaw session key used for notify routing."
)
DEFAULT_CODEX_HOME_ROOT = codex_agent_module.DEFAULT_RUNTIME_HOME_ROOT
DEFAULT_CODEX_SOURCE_HOME = codex_agent_module.DEFAULT_CODEX_SOURCE_HOME
DEFAULT_CLAUDE_COMMAND = claude_agent_module.DEFAULT_CLAUDE_COMMAND
DEFAULT_CLAUDE_SOURCE_HOME = claude_agent_module.DEFAULT_CLAUDE_SOURCE_HOME
DEFAULT_CLAUDE_SOURCE_CONFIG_PATH = claude_agent_module.DEFAULT_CLAUDE_SOURCE_CONFIG_PATH
SUPPORTED_NOTIFY_PROVIDERS = ("discord", "telegram", "tmux-bridge")
CONFIG_KEY_MAP = {
    "claude.command": "claude_command",
    "claude.home-path": "claude_home_path",
    "claude.config-path": "claude_config_path",
    "discord.bot-token": "discord_bot_token",
    "discord.mention-user-id": "notify_mention_user_id",
    "discord.webhook-url": "discord_webhook_url",
    "inline.max-sessions": "max_inline_sessions",
    "managed.ttl-seconds": "managed_session_ttl_seconds",
    "notify.enabled": "notify_enabled",
    "telegram.bot-token": "telegram_bot_token",
}


class OrcheError(RuntimeError):
    pass


class AgentStartupBlockedError(OrcheError):
    pass


def shorten(text: object, limit: int = 240) -> str:
    value = re.sub(r"\s+", " ", str(text)).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def compact_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def longest_common_prefix(before: str, after: str) -> int:
    limit = min(len(before), len(after))
    index = 0
    while index < limit and before[index] == after[index]:
        index += 1
    return index


def turn_delta(before: str, after: str) -> str:
    if before and after and before in after:
        return after.split(before, 1)[1]
    return after[longest_common_prefix(before, after) :]


def extract_summary_candidate(text: str, *, prompt: str = "") -> str:
    lines: List[str] = []
    prompt_inline = compact_text(prompt)
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("```"):
            continue
        if line.startswith(("╭", "╰", "│", "› ", "└ ")):
            continue
        if line.startswith("• "):
            line = line[2:].strip()
            if not line:
                continue
        if re.match(r"^[─━]{6,}$", line):
            continue
        if re.match(r"^[\W_─━]{20,}$", line):
            continue
        if line.startswith(("Tip:", "Command:", "Chunk ID:", "Wall time:", "Output:")):
            continue
        if line in {"Explored", "Ran", "Read", "List", "Updated Plan"}:
            continue
        if line.startswith(("Explored", "Ran ", "Read ", "List ", "Edited ")):
            continue
        if line.startswith(("OpenAI Codex", "dnq@", "^C")):
            continue
        if "gpt-" in line and "% left" in line:
            continue
        if line.startswith(("session:", "cwd:")):
            continue
        if prompt_inline and compact_text(line) == prompt_inline:
            continue
        if prompt_inline and compact_text(line).endswith(prompt_inline):
            continue
        line = compact_text(line.replace("`", ""))
        if not line:
            continue
        lines.append(line)
    return lines[-1] if lines else ""


def _is_prompt_fragment(candidate: str, prompt: str) -> bool:
    candidate_inline = compact_text(candidate)
    prompt_inline = compact_text(prompt)
    if not candidate_inline or not prompt_inline:
        return False
    return len(candidate_inline) >= 8 and candidate_inline in prompt_inline


def log_event(event: str, **fields: Any) -> None:
    ensure_directories()
    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "pid": os.getpid(),
        "event": event,
        **fields,
    }
    try:
        with orch_log_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def log_exception(event: str, exc: BaseException, **fields: Any) -> None:
    log_event(
        event,
        error_type=type(exc).__name__,
        error=str(exc),
        traceback=traceback.format_exc(),
        **fields,
    )


def slugify(text: str) -> str:
    out: List[str] = []
    for ch in text.lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in ("-", "_", "/", "."):
            out.append("-")
    value = "".join(out)
    while "--" in value:
        value = value.replace("--", "-")
    return value.strip("-") or "root"


def repo_name(cwd: Path) -> str:
    return slugify(cwd.resolve().name)


def normalize_codex_home(codex_home: Optional[Union[Path, str]]) -> str:
    return normalize_runtime_home(codex_home)


def default_session_name(cwd: Path, agent: str, purpose: str = "main") -> str:
    return f"{repo_name(cwd)}-{slugify(agent)}-{slugify(purpose)}"


def window_name(session: str) -> str:
    return f"orche-{slugify(session)}"


def tmux_session_name(session: str) -> str:
    return f"{TMUX_SESSION}-{session_key(session)}"


def session_key(session: str) -> str:
    return slugify(session)


def history_path(session: str) -> Path:
    return history_dir() / f"{session_key(session)}.jsonl"


def meta_path(session: str) -> Path:
    return meta_dir() / f"{session_key(session)}.json"


def lock_path(session: str) -> Path:
    return locks_dir() / f"{session_key(session)}.lock"


def notify_target_lock_path(session: str) -> Path:
    return locks_dir() / f"{session_key(session)}.notify.lock"


def inline_host_lock_path(tmux_session: str, host_pane_id: str = "") -> Path:
    scope = tmux_session.strip()
    host = host_pane_id.strip()
    key = f"{scope}-{host}" if host else scope
    return locks_dir() / f"inline-host-{session_key(key or 'default')}.lock"


def run(
    cmd: List[str],
    *,
    check: bool = True,
    capture: bool = False,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    input_text: Optional[str] = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        text=True,
        encoding="utf-8",
        errors="replace",
        input=input_text,
        check=check,
        capture_output=capture,
        cwd=None if cwd is None else str(cwd),
        env=env,
    )


def require_tmux() -> None:
    if shutil.which("tmux"):
        return
    raise OrcheError("tmux is not installed; orche requires tmux")


def tmux(
    *args: str,
    check: bool = True,
    capture: bool = True,
    input_text: Optional[str] = None,
) -> subprocess.CompletedProcess[str]:
    require_tmux()
    return run(["tmux", *args], check=check, capture=capture, input_text=input_text)


def _known_tmux_sessions() -> Tuple[str, ...]:
    return (TMUX_SESSION, LEGACY_TMUX_SESSION)


def _is_orche_tmux_session(name: str) -> bool:
    session_name = str(name or "").strip()
    return bool(session_name) and (
        session_name in _known_tmux_sessions() or session_name.startswith(f"{TMUX_SESSION}-")
    )


def _tmux_has_session(name: str) -> bool:
    session_name = str(name or "").strip()
    if not session_name:
        return False
    result = tmux("has-session", "-t", session_name, check=False, capture=True)
    return result.returncode == 0


def list_tmux_sessions() -> List[str]:
    result = tmux("list-sessions", "-F", "#{session_name}", check=False, capture=True)
    if result.returncode != 0:
        return []
    sessions: List[str] = []
    for line in result.stdout.splitlines():
        session_name = line.strip()
        if _is_orche_tmux_session(session_name):
            sessions.append(session_name)
    return sessions


def _bridge_result(
    args: Sequence[str],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ["tmux-bridge", *args],
        returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _resolve_bridge_pane(session: str) -> str:
    session_name = str(session or "").strip()
    if not session_name:
        raise OrcheError("session is required")
    for pane in list_panes():
        if str(pane.get("pane_title") or "").strip() == session_name:
            return str(pane.get("pane_id") or "").strip()
    meta_pane_id = str(load_meta(session_name).get("pane_id") or "").strip()
    if meta_pane_id and pane_exists(meta_pane_id):
        return meta_pane_id
    raise OrcheError(f"Unknown session: {session_name}")


def _tmux_bridge_dispatch(*args: str) -> subprocess.CompletedProcess[str]:
    if not args:
        raise OrcheError("tmux-bridge command is required")
    command = args[0]
    if command == "name":
        if len(args) != 3:
            raise OrcheError("tmux-bridge name requires <pane_id> <session>")
        pane_id, session = args[1], args[2]
        tmux("select-pane", "-t", pane_id, "-T", session, check=True, capture=True)
        return _bridge_result(args)
    if command == "resolve":
        if len(args) != 2:
            raise OrcheError("tmux-bridge resolve requires <session>")
        pane_id = _resolve_bridge_pane(args[1])
        return _bridge_result(args, stdout=pane_id)
    if command == "read":
        if len(args) != 3:
            raise OrcheError("tmux-bridge read requires <session> <lines>")
        session, line_text = args[1], args[2]
        try:
            lines = max(int(line_text), 1)
        except ValueError as exc:
            raise OrcheError(f"Invalid line count: {line_text}") from exc
        pane_id = _resolve_bridge_pane(session)
        return _bridge_result(args, stdout=read_pane(pane_id, lines))
    if command == "type":
        if len(args) != 3:
            raise OrcheError("tmux-bridge type requires <session> <text>")
        session, text = args[1], args[2]
        pane_id = _resolve_bridge_pane(session)
        buffer_name = f"orche-{uuid.uuid4().hex}"
        try:
            tmux(
                "load-buffer",
                "-b",
                buffer_name,
                "-",
                check=True,
                capture=True,
                input_text=text,
            )
            tmux("paste-buffer", "-t", pane_id, "-b", buffer_name, check=True, capture=True)
        finally:
            tmux("delete-buffer", "-b", buffer_name, check=False, capture=True)
        return _bridge_result(args)
    if command == "keys":
        if len(args) < 3:
            raise OrcheError("tmux-bridge keys requires <session> <key>...")
        session = args[1]
        pane_id = _resolve_bridge_pane(session)
        tmux("send-keys", "-t", pane_id, *args[2:], check=True, capture=True)
        return _bridge_result(args)
    raise OrcheError(f"Unsupported tmux-bridge command: {command}")


def tmux_bridge(*args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = _tmux_bridge_dispatch(*args)
    except OrcheError as exc:
        result = _bridge_result(args, returncode=1, stderr=str(exc))
        if check:
            raise subprocess.CalledProcessError(
                result.returncode,
                result.args,
                output=result.stdout,
                stderr=result.stderr,
            ) from exc
    if not capture:
        return _bridge_result(args, returncode=result.returncode)
    return result


def pane_exists(pane_id: str) -> bool:
    result = tmux("display-message", "-p", "-t", pane_id, "#{pane_id}", check=False, capture=True)
    return result.returncode == 0 and result.stdout.strip() == pane_id


def tmux_session_exists() -> bool:
    return bool(list_tmux_sessions())


def list_windows(target: Optional[str] = None) -> List[Dict[str, str]]:
    session_names = [target] if target else list_tmux_sessions()
    windows: List[Dict[str, str]] = []
    for session_name in session_names:
        result = tmux(
            "list-windows",
            "-t",
            session_name,
            "-F",
            _tmux_join_fields("#{window_id}", "#{window_name}"),
            check=False,
            capture=True,
        )
        if result.returncode != 0:
            continue
        for line in result.stdout.splitlines():
            parts = _tmux_split_fields(line, expected=2)
            if len(parts) == 2:
                windows.append({"session_name": session_name, "window_id": parts[0], "window_name": parts[1]})
    return windows


def find_window(name: str, *, target: Optional[str] = None) -> Optional[Dict[str, str]]:
    for window in list_windows(target):
        if window["window_name"] == name:
            return window
    return None


def next_window_index(session_name: str) -> int:
    result = tmux(
        "list-windows",
        "-t",
        session_name,
        "-F",
        "#{window_index}",
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        return 0
    indexes: List[int] = []
    for line in result.stdout.splitlines():
        value = line.strip()
        if not value:
            continue
        try:
            indexes.append(int(value))
        except ValueError:
            continue
    return (max(indexes) + 1) if indexes else 0


def _tmux_window_index_in_use(exc: subprocess.CalledProcessError) -> bool:
    detail = ((exc.stderr or exc.stdout or "")).strip().lower()
    return "index " in detail and " in use" in detail


def list_panes(target: Optional[str] = None) -> List[Dict[str, str]]:
    args = ["list-panes"]
    if target:
        args.extend(["-t", target])
    else:
        args.append("-a")
    args.extend(
        [
            "-F",
            _tmux_join_fields(
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
        ]
    )
    result = tmux(*args, check=False, capture=True)
    if result.returncode != 0:
        return []
    panes: List[Dict[str, str]] = []
    for line in result.stdout.splitlines():
        parts = _tmux_split_fields(line, expected=9)
        if len(parts) != 9:
            continue
        if not target and not _is_orche_tmux_session(parts[0]):
            continue
        panes.append(
            {
                "session_name": parts[0],
                "pane_id": parts[1],
                "window_id": parts[2],
                "window_name": parts[3],
                "pane_dead": parts[4],
                "pane_pid": parts[5],
                "pane_current_command": parts[6],
                "pane_current_path": parts[7],
                "pane_title": parts[8],
            }
        )
    return panes


def get_pane_info(pane_id: str) -> Optional[Dict[str, str]]:
    if not pane_exists(pane_id):
        return None
    raw = _tmux_value_for_pane(
        pane_id,
        _tmux_join_fields(
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
    )
    parts = _tmux_split_fields(raw, expected=9)
    if len(parts) != 9:
        return None
    return {
        "session_name": parts[0],
        "pane_id": parts[1],
        "window_id": parts[2],
        "window_name": parts[3],
        "pane_dead": parts[4],
        "pane_pid": parts[5],
        "pane_current_command": parts[6],
        "pane_current_path": parts[7],
        "pane_title": parts[8],
    }


def read_pane(pane_id: str, lines: int = DEFAULT_CAPTURE_LINES) -> str:
    start = f"-{max(lines, 1)}"
    result = tmux("capture-pane", "-p", "-J", "-t", pane_id, "-S", start, check=False, capture=True)
    if result.returncode != 0:
        return ""
    return "\n".join(result.stdout.splitlines()[-lines:])


def _tmux_value_for_pane(pane_id: str, fmt: str) -> str:
    result = tmux("display-message", "-p", "-t", pane_id, fmt, check=False, capture=True)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def pane_cursor_state(pane_id: str) -> Dict[str, str]:
    raw = _tmux_value_for_pane(
        pane_id,
        _tmux_join_fields("#{cursor_x}", "#{cursor_y}", "#{pane_in_mode}", "#{pane_dead}"),
    )
    parts = _tmux_split_fields(raw, expected=4)
    while len(parts) < 4:
        parts.append("")
    return {
        "cursor_x": parts[0],
        "cursor_y": parts[1],
        "pane_in_mode": parts[2],
        "pane_dead": parts[3],
    }


def process_cpu_percent(pid_text: str) -> float:
    pid = str(pid_text or "").strip()
    if not pid.isdigit():
        return 0.0
    result = run(["ps", "-o", "%cpu=", "-p", pid], check=False, capture=True)
    if result.returncode != 0:
        return 0.0
    value = (result.stdout or "").strip()
    try:
        return float(value)
    except ValueError:
        return 0.0


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _normalize_watchdog_tail(capture: str) -> str:
    tail = capture.splitlines()[-12:]
    return compact_text("\n".join(line.rstrip() for line in tail))


def _pane_signature(
    *,
    tail: str,
    cursor_x: str,
    cursor_y: str,
    pane_in_mode: str,
    pane_current_command: str,
) -> str:
    return "|".join(
        [
            tail,
            cursor_x,
            cursor_y,
            pane_in_mode,
            pane_current_command,
        ]
    )


def sample_pane_state(
    plugin: AgentPlugin,
    pane_id: str,
    *,
    capture_lines: int = WATCHDOG_CAPTURE_LINES,
) -> Dict[str, Any]:
    resolved_pane_id = str(pane_id or "").strip()
    info = get_pane_info(resolved_pane_id) if resolved_pane_id else None
    capture = read_pane(resolved_pane_id, capture_lines) if resolved_pane_id else ""
    cursor = pane_cursor_state(resolved_pane_id) if resolved_pane_id else {}
    cpu_percent = process_cpu_percent((info or {}).get("pane_pid", ""))
    tail = _normalize_watchdog_tail(capture)
    cursor_x = str(cursor.get("cursor_x") or "")
    cursor_y = str(cursor.get("cursor_y") or "")
    pane_in_mode = str(cursor.get("pane_in_mode") or "")
    pane_dead = str(cursor.get("pane_dead") or (info or {}).get("pane_dead") or "")
    pane_current_command = str((info or {}).get("pane_current_command") or "")
    return {
        "pane_id": resolved_pane_id,
        "capture": capture,
        "capture_bytes": len(capture.encode("utf-8")),
        "tail": tail,
        "signature": _pane_signature(
            tail=tail,
            cursor_x=cursor_x,
            cursor_y=cursor_y,
            pane_in_mode=pane_in_mode,
            pane_current_command=pane_current_command,
        ),
        "cursor_x": cursor_x,
        "cursor_y": cursor_y,
        "pane_in_mode": pane_in_mode,
        "pane_dead": pane_dead,
        "pane_current_command": pane_current_command,
        "cpu_percent": cpu_percent,
        "agent_running": bool(resolved_pane_id and is_agent_running(plugin, resolved_pane_id)),
    }


def sample_watchdog_state(session: str, *, pane_id: str = "") -> Dict[str, Any]:
    meta = load_meta(session)
    pending_turn = meta.get("pending_turn") if isinstance(meta.get("pending_turn"), dict) else {}
    resolved_pane_id = str(
        pane_id or bridge_resolve(session) or pending_turn.get("pane_id") or meta.get("pane_id") or ""
    ).strip()
    info = get_pane_info(resolved_pane_id) if resolved_pane_id else None
    plugin_name = str(meta.get("agent") or "codex")
    plugin = get_agent(plugin_name)
    sample = sample_pane_state(plugin, resolved_pane_id, capture_lines=WATCHDOG_CAPTURE_LINES)
    if info is not None and not sample.get("pane_current_command"):
        sample["pane_current_command"] = str(info.get("pane_current_command") or "")
    return sample


def observable_progress_detected(
    previous_signature: str,
    previous_cursor: tuple[str, str],
    sample: Mapping[str, Any],
) -> bool:
    current_cursor = (str(sample.get("cursor_x") or ""), str(sample.get("cursor_y") or ""))
    return (
        not previous_signature
        or previous_signature != str(sample.get("signature") or "")
        or previous_cursor != current_cursor
        or float(sample.get("cpu_percent") or 0.0) >= WATCHDOG_ACTIVE_CPU_THRESHOLD
    )


def recent_capture_excerpt(capture: str, *, lines: int = NOTIFY_TAIL_LINES, max_chars: int = 1200) -> str:
    excerpt = "\n".join(capture.splitlines()[-max(lines, 1) :]).strip()
    if len(excerpt) <= max_chars:
        return excerpt
    trimmed = excerpt[-max_chars:].lstrip()
    if not trimmed:
        return ""
    return f"...\n{trimmed}"


def save_meta(session: str, meta: Dict[str, Any]) -> None:
    ensure_directories()
    meta_path(session).write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_meta(session: str) -> Dict[str, Any]:
    path = meta_path(session)
    if not path.exists():
        return {}
    try:
        data = read_json_file(path)
    except (json.JSONDecodeError, JSONInputTooLargeError):
        return {}
    return data if isinstance(data, dict) else {}


def _iter_meta_payloads() -> Iterable[Dict[str, Any]]:
    ensure_directories()
    for path in sorted(meta_dir().glob("*.json")):
        try:
            payload = read_json_file(path)
        except (json.JSONDecodeError, JSONInputTooLargeError):
            continue
        if not isinstance(payload, dict):
            continue
        session = str(payload.get("session") or path.stem).strip()
        if not session:
            continue
        payload["session"] = session
        yield payload


def managed_session_ttl_seconds(config: Optional[Mapping[str, Any]] = None) -> int:
    payload = dict(config or load_config())
    raw = payload.get("managed_session_ttl_seconds", DEFAULT_MANAGED_SESSION_TTL_SECONDS)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MANAGED_SESSION_TTL_SECONDS


def max_inline_sessions(config: Optional[Mapping[str, Any]] = None) -> int:
    payload = dict(config or load_config())
    raw = payload.get("max_inline_sessions", DEFAULT_MAX_INLINE_SESSIONS)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_INLINE_SESSIONS
    if value < 1:
        return DEFAULT_MAX_INLINE_SESSIONS
    return min(value, DEFAULT_MAX_INLINE_SESSIONS)


def session_parent(meta: Mapping[str, Any]) -> str:
    return str(meta.get("parent_session") or "").strip()


def session_children(session: str, *, live_only: bool = False) -> List[str]:
    target = str(session or "").strip()
    if not target:
        return []
    children: List[str] = []
    for payload in _iter_meta_payloads():
        child_session = str(payload.get("session") or "").strip()
        if not child_session:
            continue
        if session_parent(payload) != target:
            continue
        if live_only and not session_metadata_is_live(child_session, payload):
            continue
        children.append(child_session)
    return sorted(dict.fromkeys(children))


def managed_session_last_event_at(meta: Mapping[str, Any], *, default: float = 0.0) -> float:
    for value in (
        meta.get("last_event_at"),
        meta.get("updated_at"),
        meta.get("last_seen_at"),
    ):
        try:
            numeric = float(value or 0.0)
        except (TypeError, ValueError):
            continue
        if numeric > 0.0:
            return numeric
    return default


def touch_session_event(session: str, *, source: str = "") -> Dict[str, Any]:
    session_name = str(session or "").strip()
    if not session_name:
        return {}
    with session_lock(session_name):
        meta = load_meta(session_name)
        if not meta or session_launch_mode(meta) != "managed":
            return {}
        timestamp = time.time()
        meta["last_event_at"] = timestamp
        meta["last_event_source"] = str(source or "").strip()
        meta["expires_after_seconds"] = managed_session_ttl_seconds()
        save_meta(session_name, meta)
        return {
            "last_event_at": timestamp,
            "last_event_source": meta["last_event_source"],
            "expires_after_seconds": meta["expires_after_seconds"],
        }


def _session_has_live_parent(meta: Mapping[str, Any]) -> bool:
    parent = session_parent(meta)
    if not parent:
        return False
    parent_meta = load_meta(parent)
    if not parent_meta:
        return False
    return session_metadata_is_live(parent, parent_meta)


def validate_discord_channel_id(value: str, *, option_name: str = "--channel-id") -> str:
    try:
        return common_validate_discord_channel_id(value)
    except ValueError as exc:
        message = str(exc)
        if message.startswith("--discord-channel-id"):
            message = message.replace("--discord-channel-id", option_name, 1)
        raise OrcheError(message) from exc


def validate_notify_provider(value: str, *, option_name: str = "--notify-to") -> str:
    provider = str(value or "").strip()
    if not provider:
        raise OrcheError(f"{option_name} is required")
    if provider not in SUPPORTED_NOTIFY_PROVIDERS:
        supported = ", ".join(SUPPORTED_NOTIFY_PROVIDERS)
        raise OrcheError(f"{option_name} must be one of: {supported}")
    return provider


def _read_notify_binding(payload: Mapping[str, Any]) -> Dict[str, str]:
    binding = payload.get("notify_binding")
    if isinstance(binding, Mapping):
        provider = str(binding.get("provider") or "").strip()
        target = str(binding.get("target") or "").strip()
        if provider == "discord" and target.isdigit():
            return {
                "provider": "discord",
                "target": target,
                "session": str(binding.get("session") or derive_discord_session(target)).strip(),
            }
        if provider == "tmux-bridge" and target:
            return {
                "provider": "tmux-bridge",
                "target": target,
            }
        if provider == "telegram" and target:
            return {
                "provider": "telegram",
                "target": target,
            }
    legacy_routes = payload.get("notify_routes")
    if isinstance(legacy_routes, Mapping):
        discord_route = legacy_routes.get("discord")
        if isinstance(discord_route, Mapping):
            target = str(discord_route.get("channel_id") or "").strip()
            if target.isdigit():
                return {
                    "provider": "discord",
                    "target": target,
                    "session": str(discord_route.get("session") or derive_discord_session(target)).strip(),
                }
        tmux_route = legacy_routes.get("tmux-bridge")
        if isinstance(tmux_route, Mapping):
            target = str(tmux_route.get("target_session") or tmux_route.get("target") or "").strip()
            if target:
                return {
                    "provider": "tmux-bridge",
                    "target": target,
                }
        telegram_route = legacy_routes.get("telegram")
        if isinstance(telegram_route, Mapping):
            target = str(telegram_route.get("chat_id") or telegram_route.get("target") or "").strip()
            if target:
                return {
                    "provider": "telegram",
                    "target": target,
                }
    discord_channel_id = str(payload.get("discord_channel_id") or "").strip()
    if discord_channel_id.isdigit():
        return {
            "provider": "discord",
            "target": discord_channel_id,
            "session": str(payload.get("discord_session") or derive_discord_session(discord_channel_id)).strip(),
        }
    return {}


def build_notify_binding(provider: str, target: str) -> Dict[str, str]:
    normalized_provider = validate_notify_provider(provider)
    normalized_target = str(target or "").strip()
    if normalized_provider == "discord":
        channel_id = validate_discord_channel_id(normalized_target, option_name="--notify-target")
        return {
            "provider": "discord",
            "target": channel_id,
            "session": derive_discord_session(channel_id),
        }
    if normalized_provider == "telegram":
        if not normalized_target:
            raise OrcheError("--notify-target is required for --notify-to telegram")
        return {
            "provider": "telegram",
            "target": normalized_target,
        }
    if not normalized_target:
        raise OrcheError("--notify-target is required for --notify-to tmux-bridge")
    return {
        "provider": "tmux-bridge",
        "target": normalized_target,
    }


def remove_meta(session: str) -> None:
    path = meta_path(session)
    if path.exists():
        path.unlink()


def append_history_entry(session: str, entry: Dict[str, Any]) -> None:
    ensure_directories()
    path = history_path(session)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_history_entries(session: str) -> List[Dict[str, Any]]:
    path = history_path(session)
    if not path.exists():
        return []
    if path.stat().st_size > MAX_JSON_INPUT_BYTES:
        log_event("history.read.skipped", session=session, reason="size-limit")
        return []
    entries: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = loads_json(line, source=str(path))
        except (json.JSONDecodeError, JSONInputTooLargeError):
            continue
        if isinstance(payload, dict):
            entries.append(payload)
    return entries


def list_sessions() -> List[Dict[str, Any]]:
    expire_managed_sessions()
    ensure_directories()
    sessions: List[Dict[str, Any]] = []
    for payload in _iter_meta_payloads():
        session = str(payload.get("session") or "").strip()
        if not session_metadata_is_live(session, payload):
            remove_meta(session)
            continue
        sessions.append(payload)
    sessions.sort(key=lambda item: str(item.get("session") or ""))
    return sessions


def session_exists(session: str) -> bool:
    session_name = str(session or "").strip()
    if not session_name:
        return False
    meta = load_meta(session_name)
    if meta and session_metadata_is_live(session_name, meta):
        return True
    if meta:
        remove_meta(session_name)
    if bridge_resolve(session_name):
        return True
    return _tmux_has_session(tmux_session_name(session_name))


def session_metadata_is_live(session: str, meta: Optional[Mapping[str, Any]] = None) -> bool:
    session_name = str(session or "").strip()
    if not session_name:
        return False
    payload: Mapping[str, Any] = meta or load_meta(session_name)
    if not payload:
        return False
    pane_id = str(payload.get("pane_id") or "").strip()
    if pane_id and pane_exists(pane_id):
        return True
    resolved_pane_id = bridge_resolve(session_name)
    if resolved_pane_id and pane_exists(resolved_pane_id):
        return True
    tmux_mode = str(payload.get("tmux_mode") or "").strip() or "dedicated-session"
    if tmux_mode == "inline-pane":
        return False
    target_tmux_session = str(payload.get("tmux_session") or tmux_session_name(session_name)).strip()
    return bool(target_tmux_session and _tmux_has_session(target_tmux_session))


def _managed_session_expires_at(meta: Mapping[str, Any]) -> float:
    ttl = int(meta.get("expires_after_seconds") or managed_session_ttl_seconds())
    if ttl <= 0:
        return 0.0
    last_event_at = managed_session_last_event_at(meta)
    if last_event_at <= 0.0:
        return 0.0
    return last_event_at + ttl


def expire_managed_sessions(*, now: Optional[float] = None) -> List[str]:
    timestamp = time.time() if now is None else now
    ttl = managed_session_ttl_seconds()
    if ttl <= 0:
        return []
    expired_roots: List[str] = []
    for payload in _iter_meta_payloads():
        session = str(payload.get("session") or "").strip()
        if not session or session_launch_mode(payload) != "managed":
            continue
        if not session_metadata_is_live(session, payload):
            remove_meta(session)
            continue
        if _session_has_live_parent(payload):
            continue
        expires_at = _managed_session_expires_at(payload)
        if expires_at > 0.0 and expires_at <= timestamp:
            expired_roots.append(session)
    closed: List[str] = []
    for session in sorted(dict.fromkeys(expired_roots)):
        try:
            close_session_tree(session, reason="ttl-expired")
        except Exception as exc:
            log_exception("managed_session.expire_close_failed", exc, session=session)
            continue
        closed.append(session)
    return closed


def _current_tmux_value(fmt: str) -> str:
    result = tmux("display-message", "-p", fmt, check=False, capture=True)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def default_config_values() -> Dict[str, Any]:
    return {
        "_comment": CONFIG_COMMENT,
        "claude_command": "",
        "claude_home_path": "",
        "claude_config_path": "",
        "codex_turn_complete_channel_id": "",
        "discord_bot_token": "",
        "discord_channel_id": "",
        "discord_webhook_url": "",
        "telegram_bot_token": "",
        "max_inline_sessions": DEFAULT_MAX_INLINE_SESSIONS,
        "notify_enabled": True,
        "managed_session_ttl_seconds": DEFAULT_MANAGED_SESSION_TTL_SECONDS,
        "session": "",
        "discord_session": "",
        "runtime_home": "",
        "runtime_home_managed": False,
        "runtime_label": "",
        "codex_home": "",
        "codex_home_managed": False,
        "tmux_session": "",
    }


def load_raw_config() -> Dict[str, Any]:
    ensure_directories()
    path = config_path()
    if not path.exists():
        return {}
    try:
        data = read_json_file(path)
    except (json.JSONDecodeError, JSONInputTooLargeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_config() -> Dict[str, Any]:
    merged = default_config_values()
    merged.update(load_raw_config())
    return merged


def save_config(config: Dict[str, Any]) -> None:
    ensure_directories()
    payload = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
    write_text_atomically(config_path(), payload)


def derive_discord_session(channel_id: str) -> str:
    return f"agent:main:discord:channel:{channel_id}"


def config_key_field(key: str) -> str:
    field = CONFIG_KEY_MAP.get(key)
    if field is None:
        supported = ", ".join(sorted(CONFIG_KEY_MAP))
        raise OrcheError(f"Unsupported config key: {key}. Supported keys: {supported}")
    return field


def default_config_value(key: str) -> Any:
    config_key_field(key)
    defaults = {
        "claude.command": DEFAULT_CLAUDE_COMMAND,
        "claude.home-path": "~/.claude",
        "claude.config-path": "~/.claude.json",
        "discord.bot-token": "",
        "discord.mention-user-id": "",
        "discord.webhook-url": "",
        "inline.max-sessions": DEFAULT_MAX_INLINE_SESSIONS,
        "managed.ttl-seconds": DEFAULT_MANAGED_SESSION_TTL_SECONDS,
        "notify.enabled": True,
        "telegram.bot-token": "",
    }
    return defaults[key]


def get_config_value(key: str) -> str:
    field = config_key_field(key)
    raw_config = load_raw_config()
    value = raw_config[field] if field in raw_config else default_config_value(key)
    if key == "notify.enabled":
        return "true" if bool(value) else "false"
    return "" if value is None else str(value)


def set_config_value(key: str, value: str) -> Dict[str, Any]:
    config = load_raw_config()
    field = config_key_field(key)
    normalized = value
    if key == "notify.enabled":
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            normalized = True
        elif lowered in {"0", "false", "no", "off"}:
            normalized = False
        else:
            raise OrcheError("notify.enabled must be one of: true, false, 1, 0, yes, no, on, off")
    elif key == "managed.ttl-seconds":
        try:
            normalized = int(value.strip())
        except ValueError as exc:
            raise OrcheError("managed.ttl-seconds must be an integer number of seconds") from exc
    elif key == "inline.max-sessions":
        try:
            normalized = int(value.strip())
        except ValueError as exc:
            raise OrcheError("inline.max-sessions must be an integer between 1 and 4") from exc
        if normalized < 1 or normalized > DEFAULT_MAX_INLINE_SESSIONS:
            raise OrcheError("inline.max-sessions must be between 1 and 4")
    else:
        normalized = value.strip()
    config[field] = normalized
    config["_comment"] = CONFIG_COMMENT
    save_config(config)
    return config


def reset_config_value(key: str) -> Dict[str, Any]:
    config = load_raw_config()
    field = config_key_field(key)
    config.pop(field, None)
    if config:
        config["_comment"] = CONFIG_COMMENT
    save_config(config)
    return config


def list_config_values() -> Dict[str, str]:
    return {key: get_config_value(key) for key in sorted(CONFIG_KEY_MAP)}


def update_runtime_config(
    *,
    session: str,
    cwd: Path,
    agent: str,
    pane_id: str,
    tmux_session: str = "",
    runtime_home: Optional[str] = None,
    runtime_home_managed: Optional[bool] = None,
    runtime_label: str = "",
) -> Dict[str, Any]:
    config = load_config()
    config["_comment"] = CONFIG_COMMENT
    config.pop("orch_session", None)
    config.pop("parent_session_key", None)
    config["session"] = session
    config["cwd"] = str(cwd)
    config["agent"] = agent
    config["pane_id"] = pane_id
    config["tmux_session"] = str(tmux_session or "").strip()
    normalized_runtime_home = normalize_runtime_home(runtime_home)
    config["runtime_home"] = normalized_runtime_home
    if runtime_home_managed is not None:
        config["runtime_home_managed"] = bool(runtime_home_managed)
    config["runtime_label"] = runtime_label
    if agent == "codex":
        config["codex_home"] = normalized_runtime_home
        config["codex_home_managed"] = bool(runtime_home_managed)
    else:
        config["codex_home"] = ""
        config["codex_home_managed"] = False
    config["updated_at"] = time.time()
    save_config(config)
    return config


@contextlib.contextmanager
def _path_lock(path: Path, *, timeout: float, error_message: str):
    ensure_directories()
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.time() > deadline:
                raise OrcheError(error_message)
            time.sleep(0.1)
    try:
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        os.close(fd)
        try:
            path.unlink()
        except FileNotFoundError:
            pass


@contextlib.contextmanager
def session_lock(session: str, *, timeout: float = 5.0):
    path = lock_path(session)
    with _path_lock(path, timeout=timeout, error_message=f"Timed out waiting for session lock: {session}"):
        yield


@contextlib.contextmanager
def target_session_io_lock(session: str, *, timeout: float = 5.0):
    path = notify_target_lock_path(session)
    with _path_lock(path, timeout=timeout, error_message=f"Timed out waiting for notify target lock: {session}"):
        yield


@contextlib.contextmanager
def inline_host_lock(tmux_session: str, host_pane_id: str = "", *, timeout: float = 5.0):
    path = inline_host_lock_path(tmux_session, host_pane_id)
    scope = host_pane_id.strip() or tmux_session.strip() or "inline-host"
    with _path_lock(path, timeout=timeout, error_message=f"Timed out waiting for inline host lock: {scope}"):
        yield


def bridge_name_pane(pane_id: str, session: str) -> None:
    tmux_bridge("name", pane_id, session, check=True, capture=True)


def bridge_resolve(session: str) -> Optional[str]:
    result = tmux_bridge("resolve", session, check=False, capture=True)
    if result.returncode != 0:
        return None
    pane_id = result.stdout.strip()
    return pane_id or None


def bridge_read(session: str, lines: int = DEFAULT_CAPTURE_LINES) -> str:
    result = tmux_bridge("read", session, str(lines), check=True, capture=True)
    return result.stdout.rstrip("\n")


def bridge_type(session: str, text: str) -> None:
    if not text:
        return
    tmux_bridge("read", session, "1", check=True, capture=True)
    tmux_bridge("type", session, text, check=True, capture=True)


def bridge_keys(session: str, keys: Union[Iterable[str], str]) -> None:
    values = [keys] if isinstance(keys, str) else list(keys)
    if not values:
        return
    tmux_bridge("read", session, "1", check=True, capture=True)
    tmux_bridge("keys", session, *values, check=True, capture=True)


def attach_session(session: str, *, pane_id: str = "") -> str:
    meta = load_meta(session)
    resolved_pane_id = pane_id or bridge_resolve(session) or str(meta.get("pane_id") or "")
    info = get_pane_info(resolved_pane_id) if resolved_pane_id else None
    target_tmux_session = str(meta.get("tmux_session") or "").strip()
    if info is not None:
        target_tmux_session = str(info.get("session_name") or target_tmux_session).strip()
    if not target_tmux_session:
        target_tmux_session = tmux_session_name(session)
    if not _tmux_has_session(target_tmux_session):
        raise OrcheError(f"Tmux session not found for session: {session}")
    if (
        str(meta.get("tmux_mode") or "").strip() == "inline-pane"
        and os.environ.get("TMUX")
        and _current_tmux_value("#{session_name}") == target_tmux_session
    ):
        target_window_id = str((info or {}).get("window_id") or meta.get("window_id") or "").strip()
        if target_window_id:
            tmux("select-window", "-t", target_window_id, check=False, capture=True)
        if resolved_pane_id:
            tmux("select-pane", "-t", resolved_pane_id, check=False, capture=True)
        return target_tmux_session
    if os.environ.get("TMUX"):
        result = tmux("switch-client", "-t", target_tmux_session, check=False, capture=True)
        if result.returncode != 0:
            tmux("attach-session", "-t", target_tmux_session, check=True, capture=False)
    else:
        tmux("attach-session", "-t", target_tmux_session, check=True, capture=False)
    return target_tmux_session


def list_tmux_session_clients(session_name: str) -> List[str]:
    if not _tmux_has_session(session_name):
        return []
    result = tmux("list-clients", "-t", session_name, "-F", "#{client_tty}", check=False, capture=True)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def deliver_notify_to_session(session: str, prompt: str) -> str:
    target_session = session.strip()
    if not target_session:
        raise OrcheError("notify target session is required")
    if not prompt:
        raise OrcheError("notify prompt is required")
    with target_session_io_lock(target_session):
        pane_id = bridge_resolve(target_session)
        if not pane_id:
            raise OrcheError(f"notify target session not found: {target_session}")
        target_meta = load_meta(target_session)
        target_agent = str(target_meta.get("agent") or "").strip().lower() or "codex"
        get_agent(target_agent).submit_prompt(target_session, prompt, bridge=BRIDGE)
        return pane_id


class _BridgeAdapter:
    def type(self, session: str, text: str) -> None:
        bridge_type(session, text)

    def keys(self, session: str, keys: Sequence[str]) -> None:
        bridge_keys(session, list(keys))


BRIDGE = _BridgeAdapter()


def supported_agent_names() -> Tuple[str, ...]:
    return supported_agents()


def get_agent(name: str) -> AgentPlugin:
    try:
        config = load_config()
        claude_agent_module.DEFAULT_CLAUDE_COMMAND = str(config.get("claude_command") or "").strip() or DEFAULT_CLAUDE_COMMAND
        claude_home_path = str(config.get("claude_home_path") or "").strip()
        claude_agent_module.DEFAULT_CLAUDE_SOURCE_HOME = (
            Path(claude_home_path).expanduser()
            if claude_home_path
            else DEFAULT_CLAUDE_SOURCE_HOME
        )
        claude_config_path = str(config.get("claude_config_path") or "").strip()
        claude_agent_module.DEFAULT_CLAUDE_SOURCE_CONFIG_PATH = (
            Path(claude_config_path).expanduser()
            if claude_config_path
            else DEFAULT_CLAUDE_SOURCE_CONFIG_PATH
        )
        return get_agent_plugin(name)
    except ValueError as exc:
        raise OrcheError(str(exc)) from exc


def prepare_managed_runtime(
    plugin: AgentPlugin,
    session: str,
    *,
    cwd: Path,
    discord_channel_id: Optional[str],
) -> AgentRuntime:
    try:
        if plugin.name == "codex":
            codex_agent_module.DEFAULT_RUNTIME_HOME_ROOT = DEFAULT_CODEX_HOME_ROOT
            codex_agent_module.DEFAULT_CODEX_SOURCE_HOME = DEFAULT_CODEX_SOURCE_HOME
        elif plugin.name == "claude":
            claude_agent_module.DEFAULT_RUNTIME_HOME_ROOT = default_claude_home_path(session).parent
        return plugin.ensure_managed_runtime(
            session,
            cwd=cwd,
            discord_channel_id=discord_channel_id,
        )
    except (RuntimeError, ValueError) as exc:
        raise OrcheError(str(exc)) from exc


def runtime_home_from_meta(meta: Dict[str, Any]) -> str:
    return normalize_runtime_home(meta.get("runtime_home") or meta.get("codex_home") or "")


def runtime_home_managed_from_meta(meta: Dict[str, Any]) -> bool:
    if "runtime_home_managed" in meta:
        return bool(meta.get("runtime_home_managed"))
    return bool(meta.get("codex_home_managed"))


def runtime_label_from_meta(meta: Dict[str, Any], plugin: AgentPlugin) -> str:
    return str(meta.get("runtime_label") or plugin.runtime_label)


def apply_runtime_to_meta(meta: Dict[str, Any], *, agent: str, runtime: AgentRuntime) -> None:
    meta["runtime_home"] = normalize_runtime_home(runtime.home)
    meta["runtime_home_managed"] = bool(runtime.managed)
    meta["runtime_label"] = runtime.label
    if agent == "codex":
        meta["codex_home"] = meta["runtime_home"]
        meta["codex_home_managed"] = meta["runtime_home_managed"]
    else:
        meta["codex_home"] = ""
        meta["codex_home_managed"] = False


def ensure_tmux_session(session: str, cwd: Path) -> str:
    name = tmux_session_name(session)
    if _tmux_has_session(name):
        return name
    tmux("new-session", "-d", "-s", name, "-n", window_name(session), "-c", str(cwd), check=True, capture=True)
    if not _tmux_has_session(name):
        raise OrcheError(f"Failed to create tmux session for {session}")
    return name


def _pane_record_from_tmux_output(output: str) -> Dict[str, str]:
    parts = _tmux_split_fields(output, expected=4)
    if len(parts) != 4:
        raise OrcheError("Failed to parse tmux pane output")
    return {
        "session_name": parts[0],
        "pane_id": parts[1],
        "window_id": parts[2],
        "window_name": parts[3],
        "pane_dead": "0",
        "pane_pid": "",
        "pane_current_command": "",
        "pane_current_path": "",
        "pane_title": "",
    }


def _tmux_join_fields(*parts: str) -> str:
    return TMUX_PANE_OUTPUT_SEPARATOR.join(parts)


def _tmux_split_fields(output: str, *, expected: int) -> List[str]:
    rendered = str(output or "").strip()
    if not rendered:
        return []
    parts = rendered.split(TMUX_PANE_OUTPUT_SEPARATOR)
    if len(parts) == expected:
        return parts
    parts = rendered.split("\t")
    if len(parts) == expected:
        return parts
    return []


def create_dedicated_pane(session: str, cwd: Path) -> Dict[str, str]:
    tmux_name = tmux_session_name(session)
    if _tmux_has_session(tmux_name):
        panes = list_panes(tmux_name)
        if panes:
            return panes[0]
        raise OrcheError(f"Failed to create tmux pane for {session}")
    result = tmux(
        "new-session",
        "-d",
        "-s",
        tmux_name,
        "-n",
        window_name(session),
        "-c",
        str(cwd),
        "-P",
        "-F",
        _tmux_join_fields("#{session_name}", "#{pane_id}", "#{window_id}", "#{window_name}"),
        check=True,
        capture=True,
    )
    return _pane_record_from_tmux_output(result.stdout)


def _preferred_host_pane(*, tmux_session: str, host_pane_id: str = "", exclude_pane_id: str = "") -> str:
    if host_pane_id and pane_exists(host_pane_id):
        return host_pane_id
    for pane in list_panes(tmux_session):
        pane_id = str(pane.get("pane_id") or "").strip()
        if not pane_id or pane_id == exclude_pane_id or str(pane.get("pane_dead") or "") == "1":
            continue
        return pane_id
    raise OrcheError(f"Unable to find a live host pane in tmux session: {tmux_session}")


def _inline_slot_value(value: Any) -> Optional[int]:
    try:
        slot = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if slot < 0 or slot >= DEFAULT_MAX_INLINE_SESSIONS:
        return None
    return slot


def _create_temp_inline_pane(*, tmux_session: str, cwd: Path) -> Dict[str, str]:
    last_error: Optional[subprocess.CalledProcessError] = None
    for _attempt in range(3):
        target = f"{tmux_session}:{next_window_index(tmux_session)}"
        try:
            result = tmux(
                "new-window",
                "-d",
                "-t",
                target,
                "-c",
                str(cwd),
                "-P",
                "-F",
                _tmux_join_fields("#{session_name}", "#{pane_id}", "#{window_id}", "#{window_name}"),
                check=True,
                capture=True,
            )
            return _pane_record_from_tmux_output(result.stdout)
        except subprocess.CalledProcessError as exc:
            last_error = exc
            if not _tmux_window_index_in_use(exc):
                raise
    assert last_error is not None
    raise last_error


def _inline_group_sessions(
    *,
    tmux_session: str,
    host_pane_id: str,
    exclude_session: str = "",
) -> List[Dict[str, Any]]:
    members: List[Dict[str, Any]] = []
    for payload in _iter_meta_payloads():
        child_session = str(payload.get("session") or "").strip()
        if not child_session or child_session == exclude_session:
            continue
        if str(payload.get("tmux_mode") or "").strip() != "inline-pane":
            continue
        if str(payload.get("host_pane_id") or "").strip() != host_pane_id:
            continue
        host_session = str(payload.get("tmux_host_session") or payload.get("tmux_session") or "").strip()
        if host_session != tmux_session:
            continue
        if not session_metadata_is_live(child_session, payload):
            continue
        pane_id = str(payload.get("pane_id") or "").strip()
        if not pane_id or pane_id == host_pane_id or not pane_exists(pane_id):
            continue
        info = get_pane_info(pane_id)
        if info is None or str(info.get("session_name") or "").strip() != tmux_session:
            continue
        member = dict(payload)
        member["pane_id"] = pane_id
        members.append(member)
    return members


def _normalize_inline_group_slots(group: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    def sort_key(payload: Mapping[str, Any]) -> Tuple[int, float, str]:
        slot = _inline_slot_value(payload.get("inline_slot"))
        if slot is None:
            slot = DEFAULT_MAX_INLINE_SESSIONS
        last_seen = managed_session_last_event_at(payload, default=float("inf"))
        session_name = str(payload.get("session") or "")
        return (slot, last_seen, session_name)

    normalized: List[Dict[str, Any]] = []
    for index, payload in enumerate(sorted(group, key=sort_key)):
        member = dict(payload)
        member["inline_slot"] = index
        normalized.append(member)
        session_name = str(payload.get("session") or "").strip()
        if not session_name or _inline_slot_value(payload.get("inline_slot")) == index:
            continue
        meta = load_meta(session_name)
        if not meta:
            continue
        meta["inline_slot"] = index
        save_meta(session_name, meta)
    return normalized


def _reflow_inline_panes(
    *,
    host_pane_id: str,
    pane_ids_by_slot: Mapping[int, str],
) -> None:
    """Reflow inline panes into grid layout.

    Layout strategy:
    - 1 pane: single column on right (25% width)
    - 2 panes: vertical stack on right (25% width each, 50% height)
    - 3 panes: right half split into a left stack and right full-height pane
    - 4 panes: split right half into a 2x2 grid (25% width, 50% height each cell)
    """
    host_info = get_pane_info(host_pane_id)
    if host_info is None:
        raise OrcheError(f"Unable to find host pane for inline layout: {host_pane_id}")
    host_window_id = str(host_info.get("window_id") or "").strip()

    # Count visible panes
    visible_slots = {k: v for k, v in pane_ids_by_slot.items() if v}
    pane_count = len(visible_slots)

    # Break out all existing panes first
    for pane_id in pane_ids_by_slot.values():
        if not pane_id or pane_id == host_pane_id or not pane_exists(pane_id):
            continue
        info = get_pane_info(pane_id)
        if info is not None and str(info.get("window_id") or "").strip() == host_window_id:
            tmux("break-pane", "-d", "-s", pane_id, check=True, capture=True)

    slot_0 = pane_ids_by_slot.get(0, "")
    slot_1 = pane_ids_by_slot.get(1, "")
    slot_2 = pane_ids_by_slot.get(2, "")
    slot_3 = pane_ids_by_slot.get(3, "")

    # 1 pane: single column on the right
    if pane_count == 1 and slot_0:
        tmux(
            "join-pane", "-d", "-h", "-l", "25%",
            "-s", slot_0, "-t", host_pane_id,
            check=True, capture=True,
        )
        return

    # 2 panes: vertical stack (slot 0 on top, slot 1 below)
    if pane_count == 2 and slot_0 and slot_1:
        # First pane: 25% width on the right
        tmux(
            "join-pane", "-d", "-h", "-l", "25%",
            "-s", slot_0, "-t", host_pane_id,
            check=True, capture=True,
        )
        # Second pane: vertically split from first (50% height)
        tmux(
            "join-pane", "-d", "-v", "-l", "50%",
            "-s", slot_1, "-t", slot_0,
            check=True, capture=True,
        )
        return

    # 3 panes: two stacked panes on the left of the inline region, newest pane full-height on the right.
    if pane_count == 3 and slot_0 and slot_1 and slot_2:
        tmux(
            "join-pane", "-d", "-h", "-l", "50%",
            "-s", slot_2, "-t", host_pane_id,
            check=True, capture=True,
        )
        tmux(
            "join-pane", "-d", "-h", "-l", "50%",
            "-s", slot_0, "-t", slot_2,
            check=True, capture=True,
        )
        tmux(
            "join-pane", "-d", "-v", "-l", "50%",
            "-s", slot_1, "-t", slot_0,
            check=True, capture=True,
        )
        return

    # 4 panes: split the right half into a 2x2 grid.
    if slot_0:
        tmux(
            "join-pane", "-d", "-h", "-l", "50%",
            "-s", slot_0, "-t", host_pane_id,
            check=True, capture=True,
        )
    if slot_1:
        tmux(
            "join-pane", "-d", "-h", "-l", "50%",
            "-s", slot_1, "-t", slot_0,
            check=True, capture=True,
        )
    if slot_2 and slot_0:
        tmux(
            "join-pane", "-d", "-v", "-l", "50%",
            "-s", slot_2, "-t", slot_0,
            check=True, capture=True,
        )
    if slot_3 and slot_1:
        tmux(
            "join-pane", "-d", "-v", "-l", "50%",
            "-s", slot_3, "-t", slot_1,
            check=True, capture=True,
        )


def create_inline_pane(
    session: str,
    cwd: Path,
    *,
    tmux_session: str,
    host_pane_id: str = "",
) -> Tuple[Dict[str, str], str]:
    resolved_host_pane = _preferred_host_pane(
        tmux_session=tmux_session,
        host_pane_id=host_pane_id,
    )
    existing_group = _normalize_inline_group_slots(
        _inline_group_sessions(
            tmux_session=tmux_session,
            host_pane_id=resolved_host_pane,
            exclude_session=session,
        )
    )
    inline_limit = max_inline_sessions()
    if len(existing_group) >= inline_limit:
        raise OrcheError(
            f"Inline pane limit reached for host pane {resolved_host_pane}: "
            f"{inline_limit} session(s) max. Adjust inline.max-sessions (1-4) or close an existing inline session."
        )

    new_slot = len(existing_group)
    pane = _create_temp_inline_pane(tmux_session=tmux_session, cwd=cwd)
    try:
        pane_ids_by_slot = {
            int(member["inline_slot"]): str(member.get("pane_id") or "").strip()
            for member in existing_group
            if str(member.get("pane_id") or "").strip()
        }
        pane_ids_by_slot[new_slot] = pane["pane_id"]
        _reflow_inline_panes(
            host_pane_id=resolved_host_pane,
            pane_ids_by_slot=pane_ids_by_slot,
        )
        info = get_pane_info(pane["pane_id"])
        if info is None:
            raise OrcheError(f"Failed to create inline tmux pane for {session}")
        info["inline_slot"] = str(new_slot)
        return info, resolved_host_pane
    except Exception:
        if pane_exists(pane["pane_id"]):
            tmux("kill-pane", "-t", pane["pane_id"], check=False, capture=True)
        raise


def normalize_pane(session: str, cwd: Path, pane: Dict[str, str]) -> str:
    pane_id = pane["pane_id"]
    if pane.get("pane_dead") == "1":
        tmux("respawn-pane", "-k", "-t", pane_id, "-c", str(cwd), check=True, capture=True)
    bridge_name_pane(pane_id, session)
    return pane_id


def ensure_pane(
    session: str,
    cwd: Path,
    agent: str,
    *,
    tmux_mode: str = "dedicated-session",
    host_pane_id: str = "",
    tmux_host_session: str = "",
) -> str:
    cwd = cwd.resolve()
    with session_lock(session):
        meta = load_meta(session)
        resolved_tmux_mode = str(meta.get("tmux_mode") or tmux_mode or "dedicated-session").strip() or "dedicated-session"
        resolved_host_pane_id = str(meta.get("host_pane_id") or host_pane_id or "").strip()
        resolved_tmux_host_session = str(meta.get("tmux_host_session") or tmux_host_session or "").strip()
        pane_id = str(meta.get("pane_id") or "")
        if pane_id and pane_exists(pane_id):
            info = get_pane_info(pane_id)
            if info is not None:
                pane_id = normalize_pane(session, cwd, info)
                meta.update(
                    {
                        "backend": BACKEND,
                        "session": session,
                        "cwd": str(cwd),
                        "agent": agent,
                        "tmux_session": info["session_name"],
                        "pane_id": pane_id,
                        "window_id": info["window_id"],
                        "window_name": info["window_name"],
                        "tmux_mode": resolved_tmux_mode,
                        "host_pane_id": resolved_host_pane_id,
                        "tmux_host_session": resolved_tmux_host_session,
                        "last_seen_at": time.time(),
                    }
                )
                if resolved_tmux_mode != "inline-pane":
                    meta.pop("inline_slot", None)
                save_meta(session, meta)
                return pane_id

        if resolved_tmux_mode == "inline-pane":
            inline_tmux_session = resolved_tmux_host_session or str(meta.get("tmux_session") or "").strip() or _current_tmux_value("#{session_name}")
            if not inline_tmux_session:
                raise OrcheError("Inline pane mode requires a live tmux session")
            inline_host_pane_id = resolved_host_pane_id or _current_tmux_value("#{pane_id}")
            with inline_host_lock(inline_tmux_session, inline_host_pane_id):
                pane, resolved_host_pane_id = create_inline_pane(
                    session,
                    cwd,
                    tmux_session=inline_tmux_session,
                    host_pane_id=inline_host_pane_id,
                )
                pane_id = normalize_pane(session, cwd, pane)
                meta.update(
                    {
                        "backend": BACKEND,
                        "session": session,
                        "cwd": str(cwd),
                        "agent": agent,
                        "tmux_session": pane["session_name"],
                        "pane_id": pane_id,
                        "window_id": pane["window_id"],
                        "window_name": pane["window_name"],
                        "tmux_mode": resolved_tmux_mode,
                        "host_pane_id": resolved_host_pane_id,
                        "tmux_host_session": resolved_tmux_host_session or pane["session_name"],
                        "last_seen_at": time.time(),
                    }
                )
                inline_slot = str(pane.get("inline_slot") or "").strip()
                if inline_slot:
                    meta["inline_slot"] = int(inline_slot)
                save_meta(session, meta)
                return pane_id
        else:
            pane = create_dedicated_pane(session, cwd)
        pane_id = normalize_pane(session, cwd, pane)
        meta.update(
            {
                "backend": BACKEND,
                "session": session,
                "cwd": str(cwd),
                "agent": agent,
                "tmux_session": pane["session_name"],
                "pane_id": pane_id,
                "window_id": pane["window_id"],
                "window_name": pane["window_name"],
                "tmux_mode": resolved_tmux_mode,
                "host_pane_id": resolved_host_pane_id,
                "tmux_host_session": resolved_tmux_host_session or pane["session_name"],
                "last_seen_at": time.time(),
            }
        )
        if resolved_tmux_mode == "inline-pane":
            inline_slot = str(pane.get("inline_slot") or "").strip()
            if inline_slot:
                meta["inline_slot"] = int(inline_slot)
        else:
            meta.pop("inline_slot", None)
        save_meta(session, meta)
        return pane_id


def process_descendants(root_pid: int) -> List[str]:
    result = run(["ps", "-axo", "pid=,ppid=,command="], check=False, capture=True)
    if result.returncode != 0:
        return []
    children: Dict[int, List[Tuple[int, str]]] = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append((pid, parts[2]))
    commands: List[str] = []
    stack = [root_pid]
    seen: Set[int] = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        for child_pid, command in children.get(current, []):
            commands.append(command)
            stack.append(child_pid)
    return commands


def is_agent_running(plugin: AgentPlugin, pane_id: str) -> bool:
    info = get_pane_info(pane_id)
    if info is None or info.get("pane_dead") == "1":
        return False
    command = (info.get("pane_current_command") or "").lower()
    try:
        pane_pid = int(info.get("pane_pid") or "0")
    except ValueError:
        return False
    return plugin.matches_process(command, process_descendants(pane_pid))


def wait_for_agent_ready(plugin: AgentPlugin, pane_id: str, cwd: Path, *, timeout: float = STARTUP_TIMEOUT) -> str:
    deadline = time.time() + timeout
    ready_streak = 0
    last_signature = ""
    last_cursor = ("", "")
    last_sample: Dict[str, Any] = {}
    while time.time() <= deadline:
        sample = sample_pane_state(plugin, pane_id, capture_lines=DEFAULT_CAPTURE_LINES)
        capture = str(sample.get("capture") or "")
        if any(prompt in capture for prompt in plugin.login_prompts):
            raise OrcheError(f"{plugin.display_name} is not logged in inside the tmux pane")
        if str(sample.get("pane_dead") or "") == "1":
            raise OrcheError(f"{plugin.display_name} pane exited before becoming ready: {pane_id}")
        ready_candidate = bool(sample.get("agent_running")) and plugin.capture_has_ready_surface(capture, cwd)
        ready_streak = ready_streak + 1 if ready_candidate else 0
        if ready_streak >= plugin.ready_streak_required:
            return pane_id
        _ = observable_progress_detected(last_signature, last_cursor, sample)
        last_signature = str(sample.get("signature") or "")
        last_cursor = (str(sample.get("cursor_x") or ""), str(sample.get("cursor_y") or ""))
        last_sample = sample
        time.sleep(1.0)
    if bool(last_sample.get("agent_running")):
        raise AgentStartupBlockedError(f"{plugin.display_name} startup blocked before reaching ready state in {pane_id}")
    raise OrcheError(f"Timed out waiting for {plugin.display_name} to become ready in {pane_id}")


def wait_for_managed_startup_ready(
    session: str,
    plugin: AgentPlugin,
    pane_id: str,
    cwd: Path,
    *,
    timeout: float = STARTUP_TIMEOUT,
) -> str:
    deadline = time.time() + timeout
    ready_streak = 0
    while time.time() <= deadline:
        meta = load_meta(session)
        startup = meta.get("startup") if isinstance(meta.get("startup"), dict) else {}
        startup_state = str(startup.get("state") or "").strip().lower()
        if startup_state == "ready":
            if plugin.name == "claude":
                ready_at = float(startup.get("ready_at") or startup.get("updated_at") or startup.get("started_at") or 0.0)
                if ready_at > 0 and (time.time() - ready_at) < CLAUDE_STARTUP_GRACE_SECONDS:
                    time.sleep(0.1)
                    continue
            return pane_id
        if startup_state == "blocked":
            blocked_reason = str(startup.get("blocked_reason") or "").strip()
            detail = blocked_reason or f"{plugin.display_name} startup blocked before reaching ready state in {pane_id}"
            raise AgentStartupBlockedError(detail)
        if startup_state == "timeout":
            blocked_reason = str(startup.get("blocked_reason") or "").strip()
            detail = blocked_reason or f"{plugin.display_name} startup timed out before reaching ready state in {pane_id}"
            raise AgentStartupBlockedError(detail)
        capture = read_pane(pane_id, DEFAULT_CAPTURE_LINES)
        if any(prompt in capture for prompt in plugin.login_prompts):
            raise OrcheError(f"{plugin.display_name} is not logged in inside the tmux pane")
        ready_candidate = plugin.name == "codex" and plugin.capture_has_ready_surface(capture, cwd)
        ready_streak = ready_streak + 1 if ready_candidate else 0
        if ready_streak >= plugin.ready_streak_required:
            mark_session_startup_ready(session, source="ready-surface-fallback")
            return pane_id
        info = get_pane_info(pane_id)
        if info is None or info.get("pane_dead") == "1":
            raise OrcheError(f"{plugin.display_name} pane exited before startup completed: {pane_id}")
        if not is_agent_running(plugin, pane_id):
            raise OrcheError(f"{plugin.display_name} process exited before startup completed: {pane_id}")
        time.sleep(0.5)
    reason = f"Timed out waiting for {plugin.display_name} SessionStart(startup) hook in {pane_id}"
    mark_session_startup_timeout(session, reason=reason)
    raise OrcheError(reason)


def _managed_startup_reuse_wait_policy(
    session: str,
    plugin: AgentPlugin,
    pane_id: str,
    startup: Mapping[str, Any],
) -> bool:
    startup_state = str(startup.get("state") or "").strip().lower()
    if not startup_state:
        mark_session_startup_ready(session, source="existing-running-process")
        return False
    if startup_state == "ready":
        return False
    if startup_state == "launching":
        return True
    if startup_state in {"blocked", "timeout"}:
        detail = str(startup.get("blocked_reason") or "").strip()
        if not detail:
            if startup_state == "timeout":
                detail = f"{plugin.display_name} startup timed out before reaching ready state in {pane_id}"
            else:
                detail = f"{plugin.display_name} startup blocked before reaching ready state in {pane_id}"
        raise OrcheError(
            f"Session {session} is not ready because {detail}. "
            "Reopen or restart the session before sending prompts."
        )
    return False


def wait_for_claude_startup_ready(
    session: str,
    plugin: AgentPlugin,
    pane_id: str,
    cwd: Path,
    *,
    timeout: float = STARTUP_TIMEOUT,
) -> str:
    return wait_for_managed_startup_ready(session, plugin, pane_id, cwd, timeout=timeout)


def wait_for_agent_process_start(
    plugin: AgentPlugin,
    pane_id: str,
    *,
    timeout: float = STARTUP_TIMEOUT,
) -> str:
    deadline = time.time() + timeout
    last_capture = ""
    while time.time() <= deadline:
        capture = read_pane(pane_id, DEFAULT_CAPTURE_LINES)
        last_capture = capture
        launch_error = extract_launch_error(capture)
        if launch_error:
            raise OrcheError(launch_error)
        if any(prompt in capture for prompt in plugin.login_prompts):
            return pane_id
        info = get_pane_info(pane_id)
        if info is None or info.get("pane_dead") == "1":
            launch_error = extract_launch_error(last_capture)
            if launch_error:
                raise OrcheError(launch_error)
            raise OrcheError(f"{plugin.display_name} pane exited before launch completed: {pane_id}")
        if is_agent_running(plugin, pane_id):
            return pane_id
        time.sleep(0.5)
    launch_error = extract_launch_error(last_capture)
    if launch_error:
        raise OrcheError(launch_error)
    raise OrcheError(f"Timed out waiting for {plugin.display_name} process to start in {pane_id}")


def ensure_agent_running(
    plugin: AgentPlugin,
    session: str,
    cwd: Path,
    pane_id: str,
    *,
    approve_all: bool = False,
    runtime: Optional[AgentRuntime] = None,
    discord_channel_id: Optional[str] = None,
) -> str:
    if is_agent_running(plugin, pane_id):
        return pane_id
    approve_all = True
    info = get_pane_info(pane_id)
    if info is None:
        raise OrcheError(f"Pane disappeared before {plugin.display_name} launch: {pane_id}")
    try:
        launch_command = plugin.build_launch_command(
            approve_all=approve_all,
            cwd=cwd,
            runtime=runtime or AgentRuntime(label=plugin.runtime_label),
            session=session,
            discord_channel_id=discord_channel_id,
        )
    except (RuntimeError, ValueError) as exc:
        raise OrcheError(str(exc)) from exc
    tmux(
        "respawn-pane",
        "-k",
        "-t",
        pane_id,
        "-c",
        str(cwd),
        launch_command,
        check=True,
        capture=True,
    )
    pane_id = wait_for_agent_process_start(plugin, pane_id)
    bridge_name_pane(pane_id, session)
    meta = load_meta(session)
    meta.update(
        {
            "backend": BACKEND,
            "session": session,
            "cwd": str(cwd),
            "pane_id": pane_id,
            "agent_started_at": time.time(),
            "agent_approve_all": approve_all,
            "last_seen_at": time.time(),
        }
    )
    apply_runtime_to_meta(meta, agent=plugin.name, runtime=runtime or AgentRuntime(label=plugin.runtime_label))
    save_meta(session, meta)
    return pane_id


def is_codex_running(pane_id: str) -> bool:
    return is_agent_running(get_agent("codex"), pane_id)


def ensure_codex_running(
    session: str,
    cwd: Path,
    pane_id: str,
    *,
    approve_all: bool = False,
    codex_home: Optional[str] = None,
    discord_channel_id: Optional[str] = None,
) -> str:
    return ensure_agent_running(
        get_agent("codex"),
        session,
        cwd,
        pane_id,
        approve_all=approve_all,
        runtime=AgentRuntime(home=normalize_codex_home(codex_home), managed=False, label=get_agent("codex").runtime_label),
        discord_channel_id=discord_channel_id,
    )


def append_action_history(session: str, cwd: Path, agent: str, action: str, **fields: Any) -> None:
    append_history_entry(
        session,
        {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "backend": BACKEND,
            "session": session,
            "cwd": str(cwd),
            "agent": agent,
            "action": action,
            **fields,
        },
    )
    if action != "close":
        touch_session_event(session, source=f"action:{action}")


def ensure_managed_codex_home(session: str, *, cwd: Path, discord_channel_id: Optional[str]) -> Path:
    codex_agent_module.DEFAULT_RUNTIME_HOME_ROOT = DEFAULT_CODEX_HOME_ROOT
    codex_agent_module.DEFAULT_CODEX_SOURCE_HOME = DEFAULT_CODEX_SOURCE_HOME
    runtime = prepare_managed_runtime(get_agent("codex"), session, cwd=cwd, discord_channel_id=discord_channel_id)
    return Path(runtime.home)


def ensure_managed_claude_home(session: str, *, cwd: Path, discord_channel_id: Optional[str]) -> Path:
    runtime = prepare_managed_runtime(get_agent("claude"), session, cwd=cwd, discord_channel_id=discord_channel_id)
    return Path(runtime.home)


def remove_managed_codex_home(codex_home: str) -> None:
    if codex_home:
        remove_runtime_home(codex_home)


def session_launch_mode(meta: Mapping[str, Any]) -> str:
    mode = str(meta.get("launch_mode") or "").strip()
    return mode or "managed"


def native_cli_args_from_meta(meta: Mapping[str, Any]) -> List[str]:
    raw_args = meta.get("native_cli_args")
    if not isinstance(raw_args, list):
        return []
    values: List[str] = []
    for value in raw_args:
        text = str(value)
        if text:
            values.append(text)
    return values


def build_native_agent_launch_command(
    plugin: AgentPlugin,
    *,
    session: str,
    cwd: Path,
    cli_args: Sequence[str],
) -> str:
    command_tokens = plugin.command_tokens()
    binary = command_tokens[0]
    command = [*command_tokens, *plugin.native_launch_args(cwd=cwd, cli_args=cli_args)]
    prefix = [f"cd {shlex.quote(str(cwd))}"]
    orche_shim = ensure_orche_shim()
    prefix.append(f"export ORCHE_BIN={shlex.quote(str(orche_shim))}")
    prefix.append(f"export PATH={shlex.quote(str(orche_shim.parent))}:$PATH")
    if session:
        prefix.append(f"export ORCHE_SESSION={shlex.quote(session)}")
    launch_error = (
        f"{LAUNCH_ERROR_PREFIX} {plugin.display_name} CLI not found in PATH. "
        f"Install {binary} or add it to PATH."
    )
    prefix.append(
        "if ! command -v "
        f"{shlex.quote(binary)} >/dev/null 2>&1; "
        f"then printf '%s\\n' {shlex.quote(launch_error)} >&2; sleep 2; exit 127; fi"
    )
    prefix.append(f"exec {' '.join(shlex.quote(part) for part in command)}")
    return " && ".join(prefix)


def extract_launch_error(capture: str) -> str:
    for line in capture.splitlines():
        text = str(line or "").strip()
        if text.startswith(LAUNCH_ERROR_PREFIX):
            return text[len(LAUNCH_ERROR_PREFIX) :].strip()
    return ""


def ensure_native_agent_running(
    plugin: AgentPlugin,
    session: str,
    cwd: Path,
    pane_id: str,
    *,
    cli_args: Sequence[str],
) -> str:
    if is_agent_running(plugin, pane_id):
        return pane_id
    info = get_pane_info(pane_id)
    if info is None:
        raise OrcheError(f"Pane disappeared before {plugin.display_name} launch: {pane_id}")
    launch_command = build_native_agent_launch_command(
        plugin,
        session=session,
        cwd=cwd,
        cli_args=cli_args,
    )
    tmux(
        "respawn-pane",
        "-k",
        "-t",
        pane_id,
        "-c",
        str(cwd),
        launch_command,
        check=True,
        capture=True,
    )
    pane_id = wait_for_agent_process_start(plugin, pane_id)
    bridge_name_pane(pane_id, session)
    meta = load_meta(session)
    meta.update(
        {
            "backend": BACKEND,
            "session": session,
            "cwd": str(cwd),
            "pane_id": pane_id,
            "agent_started_at": time.time(),
            "last_seen_at": time.time(),
        }
    )
    save_meta(session, meta)
    return pane_id


def ensure_native_session(
    session: str,
    cwd: Path,
    agent: str,
    *,
    cli_args: Sequence[str] = (),
) -> str:
    cwd = cwd.resolve()
    plugin = get_agent(agent)
    existing_meta = load_meta(session)
    existing_cwd = Path(str(existing_meta.get("cwd") or "")).resolve() if existing_meta.get("cwd") else None
    if existing_cwd is not None and existing_cwd != cwd:
        raise OrcheError(
            f"Session {session} is already bound to cwd={existing_cwd}. "
            "Use the same --cwd or close the session and create a new one."
        )
    existing_agent = str(existing_meta.get("agent") or "").strip()
    if existing_agent and existing_agent != plugin.name:
        raise OrcheError(
            f"Session {session} is already bound to agent={existing_agent}. "
            "Close the session and create a new one for a different agent."
        )
    if existing_meta and session_launch_mode(existing_meta) != "native":
        raise OrcheError(
            f"Session {session} is already managed by orche open. "
            "Use orche open without raw agent args for managed sessions, or close the session and recreate it."
        )
    provided_cli_args = [str(value) for value in cli_args]
    existing_cli_args = native_cli_args_from_meta(existing_meta)
    if existing_meta and provided_cli_args and provided_cli_args != existing_cli_args:
        raise OrcheError(
            f"Session {session} is already bound to native args={existing_cli_args!r}. "
            "Use the same shortcut args or close the session and create a new one."
        )
    resolved_cli_args = existing_cli_args or provided_cli_args
    pane_id = ensure_pane(session, cwd, agent)
    pane_id = ensure_native_agent_running(
        plugin,
        session,
        cwd,
        pane_id,
        cli_args=resolved_cli_args,
    )
    meta = load_meta(session)
    meta.update(
        {
            "backend": BACKEND,
            "session": session,
            "cwd": str(cwd),
            "agent": agent,
            "pane_id": pane_id,
            "launch_mode": "native",
            "native_cli_args": list(resolved_cli_args),
            "last_seen_at": time.time(),
            "runtime_home": "",
            "runtime_home_managed": False,
            "runtime_label": "",
            "codex_home": "",
            "codex_home_managed": False,
            "parent_session": "",
            "last_event_at": 0.0,
            "last_event_source": "",
            "expires_after_seconds": 0,
        }
    )
    meta.pop("discord_channel_id", None)
    meta.pop("discord_session", None)
    meta.pop("notify_routes", None)
    meta.pop("notify_binding", None)
    save_meta(session, meta)
    update_runtime_config(
        session=session,
        cwd=cwd,
        agent=agent,
        pane_id=pane_id,
        tmux_session=str(meta.get("tmux_session") or ""),
        runtime_home="",
        runtime_home_managed=False,
        runtime_label="",
    )
    return pane_id


def _turn_matches(turn: Mapping[str, Any], *, turn_id: str = "", prompt: str = "") -> bool:
    if turn_id and str(turn.get("turn_id") or "") == str(turn_id):
        return True
    if prompt and str(turn.get("prompt") or "") == str(prompt):
        return True
    return False


def _current_turn_entry(
    meta: Mapping[str, Any],
    turn_id: str = "",
    *,
    prompt: str = "",
    allow_fallback: bool = True,
) -> Tuple[str, Dict[str, Any]]:
    pending_turn = meta.get("pending_turn") if isinstance(meta.get("pending_turn"), dict) else None
    last_completed_turn = meta.get("last_completed_turn") if isinstance(meta.get("last_completed_turn"), dict) else None
    if turn_id or prompt:
        if pending_turn and _turn_matches(pending_turn, turn_id=turn_id, prompt=prompt):
            return "pending_turn", dict(pending_turn)
        if last_completed_turn and _turn_matches(last_completed_turn, turn_id=turn_id, prompt=prompt):
            return "last_completed_turn", dict(last_completed_turn)
        if not allow_fallback:
            return "", {}
    if pending_turn:
        return "pending_turn", dict(pending_turn)
    if last_completed_turn:
        return "last_completed_turn", dict(last_completed_turn)
    return "", {}


def initialize_session_startup(
    session: str,
    *,
    state: str = "launching",
    started_at: float | None = None,
) -> Dict[str, Any]:
    timestamp = started_at if started_at is not None else time.time()
    with session_lock(session):
        meta = load_meta(session)
        startup = {
            "state": state,
            "started_at": timestamp,
            "ready_at": 0.0,
            "ready_source": "",
            "blocked_at": 0.0,
            "blocked_reason": "",
            "blocked_event": "",
            "updated_at": timestamp,
        }
        meta["startup"] = startup
        save_meta(session, meta)
    touch_session_event(session, source=f"startup:{state}")
    return dict(startup)


def mark_session_startup_ready(session: str, *, source: str) -> Dict[str, Any]:
    timestamp = time.time()
    with session_lock(session):
        meta = load_meta(session)
        startup = dict(meta.get("startup") or {})
        startup.update(
            {
                "state": "ready",
                "ready_at": float(startup.get("ready_at") or timestamp),
                "ready_source": str(source or "").strip(),
                "blocked_at": 0.0,
                "blocked_reason": "",
                "blocked_event": "",
                "updated_at": timestamp,
            }
        )
        if not startup.get("started_at"):
            startup["started_at"] = timestamp
        meta["startup"] = startup
        save_meta(session, meta)
    touch_session_event(session, source=f"startup:ready:{source}")
    return dict(startup)


def mark_session_startup_timeout(session: str, *, reason: str = "") -> Dict[str, Any]:
    timestamp = time.time()
    with session_lock(session):
        meta = load_meta(session)
        startup = dict(meta.get("startup") or {})
        startup.update(
            {
                "state": "timeout",
                "blocked_reason": str(reason or startup.get("blocked_reason") or "").strip(),
                "updated_at": timestamp,
            }
        )
        if not startup.get("started_at"):
            startup["started_at"] = timestamp
        meta["startup"] = startup
        save_meta(session, meta)
    touch_session_event(session, source="startup:timeout")
    return dict(startup)


def mark_session_startup_blocked(
    session: str,
    *,
    reason: str,
    event_name: str,
) -> Tuple[Dict[str, Any], bool]:
    timestamp = time.time()
    with session_lock(session):
        meta = load_meta(session)
        startup = dict(meta.get("startup") or {})
        state = str(startup.get("state") or "").strip().lower()
        if state != "launching":
            return startup, False
        blocked_reason = str(reason or "").strip()
        blocked_event = str(event_name or "").strip()
        changed = (
            str(startup.get("blocked_reason") or "") != blocked_reason
            or str(startup.get("blocked_event") or "") != blocked_event
            or state != "blocked"
        )
        startup.update(
            {
                "state": "blocked",
                "blocked_at": timestamp,
                "blocked_reason": blocked_reason,
                "blocked_event": blocked_event,
                "updated_at": timestamp,
            }
        )
        if not startup.get("started_at"):
            startup["started_at"] = timestamp
        meta["startup"] = startup
        save_meta(session, meta)
    touch_session_event(session, source=f"startup:blocked:{blocked_event or 'unknown'}")
    return dict(startup), changed


def mark_pending_turn_prompt_accepted(session: str, *, source: str = "user-prompt-submit") -> Dict[str, Any]:
    timestamp = time.time()
    with session_lock(session):
        meta = load_meta(session)
        pending_turn = meta.get("pending_turn") if isinstance(meta.get("pending_turn"), dict) else None
        if not pending_turn:
            return {}
        prompt_ack = dict(pending_turn.get("prompt_ack") or {})
        prompt_ack.update(
            {
                "state": "accepted",
                "accepted_at": timestamp,
                "source": str(source or "").strip(),
            }
        )
        pending_turn["prompt_ack"] = prompt_ack
        meta["pending_turn"] = pending_turn
        save_meta(session, meta)
    touch_session_event(session, source=f"prompt-ack:{source}")
    return dict(prompt_ack)


def wait_for_prompt_ack(
    session: str,
    *,
    turn_id: str,
    prompt: str,
    timeout: float = CLAUDE_PROMPT_ACK_TIMEOUT,
) -> Dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() <= deadline:
        meta = load_meta(session)
        _turn_key, turn = _current_turn_entry(meta, turn_id=turn_id, prompt=prompt, allow_fallback=False)
        prompt_ack = turn.get("prompt_ack") if isinstance(turn.get("prompt_ack"), dict) else {}
        if str(prompt_ack.get("state") or "").strip().lower() == "accepted":
            return dict(prompt_ack)
        time.sleep(CLAUDE_PROMPT_ACK_POLL_INTERVAL)
    raise OrcheError(
        f"Timed out waiting for Claude to accept the prompt in {session}; the prompt may have been submitted before the TUI was ready"
    )


def claim_turn_notification(
    session: str,
    event: str,
    *,
    turn_id: str = "",
    prompt: str = "",
    source: str = "",
    status: str = "",
    summary: str = "",
    notification_key: str = "",
) -> bool:
    normalized_event = str(notification_key or event or "").strip().lower()
    if not session or not normalized_event:
        return True
    with session_lock(session):
        meta = load_meta(session)
        strict_match = str(event or "").strip().lower() == "completed" and bool(turn_id or prompt)
        turn_key, turn = _current_turn_entry(
            meta,
            turn_id=turn_id,
            prompt=prompt,
            allow_fallback=not strict_match,
        )
        if not turn_key or not turn:
            return True
        notifications = turn.get("notifications")
        if not isinstance(notifications, dict):
            notifications = {}
        if normalized_event in notifications:
            return False
        notifications[normalized_event] = {
            "at": time.time(),
            "source": str(source or "").strip(),
            "status": str(status or "").strip(),
            "summary": shorten(summary, 400),
        }
        turn["notifications"] = notifications
        meta[turn_key] = turn
        save_meta(session, meta)
    touch_session_event(session, source=f"notify-claim:{normalized_event}")
    return True


def release_turn_notification(
    session: str,
    event: str,
    *,
    turn_id: str = "",
    prompt: str = "",
    notification_key: str = "",
) -> None:
    normalized_event = str(notification_key or event or "").strip().lower()
    if not session or not normalized_event:
        return
    with session_lock(session):
        meta = load_meta(session)
        strict_match = str(event or "").strip().lower() == "completed" and bool(turn_id or prompt)
        turn_key, turn = _current_turn_entry(
            meta,
            turn_id=turn_id,
            prompt=prompt,
            allow_fallback=not strict_match,
        )
        if not turn_key or not turn:
            return
        notifications = turn.get("notifications")
        if not isinstance(notifications, dict) or normalized_event not in notifications:
            return
        notifications.pop(normalized_event, None)
        turn["notifications"] = notifications
        meta[turn_key] = turn
        save_meta(session, meta)
    touch_session_event(session, source=f"notify-release:{normalized_event}")


def update_watchdog_metadata(
    session: str,
    *,
    turn_id: str,
    values: Mapping[str, Any],
) -> Dict[str, Any]:
    with session_lock(session):
        meta = load_meta(session)
        pending_turn = meta.get("pending_turn") if isinstance(meta.get("pending_turn"), dict) else None
        if not pending_turn or str(pending_turn.get("turn_id") or "") != turn_id:
            return {}
        watchdog = pending_turn.get("watchdog")
        if not isinstance(watchdog, dict):
            watchdog = {}
        watchdog.update(values)
        pending_turn["watchdog"] = watchdog
        meta["pending_turn"] = pending_turn
        save_meta(session, meta)
    touch_session_event(session, source="watchdog")
    return dict(watchdog)


def _orche_bootstrap_command() -> List[str]:
    return orche_bootstrap_command()


def emit_internal_notify(
    session: str,
    *,
    event: str,
    summary: str,
    status: str,
    turn_id: str = "",
    cwd: str = "",
    source: str = "",
    notification_key: str = "",
    tail_text: str = "",
) -> bool:
    notify_provider = _read_notify_binding(load_meta(session)).get("provider", "")
    normalized_tail_text = recent_capture_excerpt(tail_text) if notify_provider == "discord" else tail_text.strip()
    payload = {
        "event": event,
        "summary": summary,
        "session": session,
        "cwd": cwd,
        "turn_id": turn_id,
        "source": source,
        "metadata": {
            "turn_id": turn_id,
            "source": source,
            "notification_key": notification_key,
        },
    }
    if normalized_tail_text:
        payload["metadata"]["tail_text"] = normalized_tail_text
        payload["metadata"]["tail_lines"] = max(1, len(normalized_tail_text.splitlines()))
    command = _orche_bootstrap_command() + ["notify-internal", "--session", session, "--status", status]
    result = subprocess.run(
        command,
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        capture_output=True,
        check=False,
        start_new_session=True,
    )
    if result.returncode != 0:
        log_event(
            "watchdog.notify.failed",
            session=session,
            notify_event=event,
            status=status,
            detail=shorten((result.stderr or result.stdout or "").strip(), 400),
        )
        return False
    touch_session_event(session, source=f"notify:{event}")
    return True


def _watchdog_summary_for_event(
    event: str,
    *,
    pending_turn: Mapping[str, Any],
    capture: str,
) -> str:
    before_capture = str(pending_turn.get("before_capture") or "")
    delta = turn_delta(before_capture, capture) if capture else ""
    prompt = str(pending_turn.get("prompt") or "")
    candidate = extract_summary_candidate(delta, prompt=prompt)
    if candidate and not _is_prompt_fragment(candidate, prompt):
        return candidate
    if event == "failed":
        return "Agent process exited before completion notify was delivered"
    if event == "needs-input":
        return "Agent has been idle for an extended period and likely needs input"
    return "Agent output has stalled without observable progress"


def _watchdog_event_status(event: str) -> str:
    if event == "failed":
        return "failure"
    if event in {"stalled", "needs-input", "startup-blocked"}:
        return event
    return "success"


def _should_use_inline_pane(notify_binding: Mapping[str, Any]) -> Tuple[bool, str, str]:
    if str(notify_binding.get("provider") or "").strip() != "tmux-bridge":
        return False, "", ""
    current_tmux_session = _current_tmux_value("#{session_name}")
    current_pane_id = _current_tmux_value("#{pane_id}")
    if not current_tmux_session or not current_pane_id:
        return False, "", ""
    with contextlib.suppress(Exception):
        current_session = current_session_id()
        if current_session and str(notify_binding.get("target") or "").strip() == current_session:
            return True, current_tmux_session, current_pane_id
    return False, "", ""


def _pending_turn_completion_summary(
    plugin: AgentPlugin,
    *,
    pending_turn: Mapping[str, Any],
    capture: str,
) -> str:
    before_capture = str(pending_turn.get("before_capture") or "")
    prompt = str(pending_turn.get("prompt") or "")
    return _completion_summary_from_capture(
        plugin,
        capture=capture,
        before_capture=before_capture,
        prompt=prompt,
    )


def _completion_summary_from_capture(
    plugin: AgentPlugin,
    *,
    capture: str,
    before_capture: str,
    prompt: str,
) -> str:
    delta = turn_delta(before_capture, capture) if capture else ""
    for candidate in (delta, capture):
        if not candidate:
            continue
        summary = plugin.extract_completion_summary(candidate, prompt)
        if summary:
            return summary
        if plugin.capture_has_completion_surface(candidate, prompt):
            fallback = extract_summary_candidate(candidate, prompt=prompt)
            if fallback:
                return fallback
    return ""


def _latest_notification_at(pending_turn: Mapping[str, Any]) -> float:
    notifications = pending_turn.get("notifications")
    if not isinstance(notifications, dict):
        return 0.0
    latest = 0.0
    for payload in notifications.values():
        if not isinstance(payload, Mapping):
            continue
        try:
            latest = max(latest, float(payload.get("at") or 0.0))
        except (TypeError, ValueError):
            continue
    return latest


def _watchdog_time_value(*values: Any, default: float) -> float:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return default


def _watchdog_reminder_summary(session: str, state: str) -> str:
    normalized = str(state or "").strip().lower() or "stalled"
    if normalized == "needs-input":
        situation = "The agent is likely waiting for terminal input, approval, or other user intervention."
    else:
        situation = "The agent session has shown no visible progress for an extended period."
    return (
        f"Session {session} is still in {normalized} state and has gone 10 minutes without a successful notify. "
        f"{situation} To reconnect with it, run `orche status {session}` and "
        f"`orche read {session} --lines 120`."
    )


def _watchdog_pending_event_ready(
    watchdog: Mapping[str, Any],
    *,
    event: str,
    summary: str,
    now: float,
    notify_buffer: float,
) -> tuple[bool, dict[str, Any]]:
    if notify_buffer <= 0:
        return True, {
            "pending_event": "",
            "pending_event_at": 0.0,
            "pending_event_summary": "",
        }
    pending_event = str(watchdog.get("pending_event") or "")
    pending_summary = str(watchdog.get("pending_event_summary") or "")
    pending_at = float(watchdog.get("pending_event_at") or 0.0)
    if pending_event != event or pending_summary != summary or pending_at <= 0.0:
        return False, {
            "pending_event": event,
            "pending_event_at": now,
            "pending_event_summary": summary,
        }
    if now - pending_at < notify_buffer:
        return False, {}
    return True, {
        "pending_event": "",
        "pending_event_at": 0.0,
        "pending_event_summary": "",
    }


def start_session_watchdog(session: str, *, turn_id: str = "") -> int:
    meta = load_meta(session)
    turn_key, turn = _current_turn_entry(meta, turn_id=turn_id)
    if turn_key != "pending_turn" or not turn:
        raise OrcheError(f"Session {session} has no pending turn to watch")
    resolved_turn_id = str(turn.get("turn_id") or "").strip()
    watchdog = turn.get("watchdog") if isinstance(turn.get("watchdog"), dict) else {}
    existing_pid = int(watchdog.get("pid") or 0) if str(watchdog.get("pid") or "").isdigit() else 0
    if existing_pid and process_is_alive(existing_pid):
        return existing_pid
    command = _orche_bootstrap_command() + ["watchdog-loop-internal", "--session", session, "--turn-id", resolved_turn_id]
    proc = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    update_watchdog_metadata(
        session,
        turn_id=resolved_turn_id,
        values={
            "pid": proc.pid,
            "state": "starting",
            "started_at": time.time(),
            "last_progress_at": _watchdog_time_value(turn.get("submitted_at"), default=time.time()),
            "last_sample_at": 0.0,
            "idle_samples": 0,
            "stop_requested": False,
        },
    )
    return proc.pid


def stop_session_watchdog(session: str) -> int:
    meta = load_meta(session)
    turn_key, turn = _current_turn_entry(meta)
    if turn_key != "pending_turn" or not turn:
        return 0
    watchdog = turn.get("watchdog") if isinstance(turn.get("watchdog"), dict) else {}
    pid = int(watchdog.get("pid") or 0) if str(watchdog.get("pid") or "").isdigit() else 0
    update_watchdog_metadata(
        session,
        turn_id=str(turn.get("turn_id") or ""),
        values={
            "stop_requested": True,
            "stopped_at": time.time(),
            "state": "stopping",
        },
    )
    if pid > 0 and process_is_alive(pid):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
    return pid


def complete_pending_turn(
    session: str,
    *,
    summary: str = "",
    turn_id: str = "",
    prompt: str = "",
    completed_at: float | None = None,
) -> Dict[str, Any]:
    finished_at = completed_at if completed_at is not None else time.time()
    with session_lock(session):
        meta = load_meta(session)
        pending_turn = meta.get("pending_turn") if isinstance(meta.get("pending_turn"), dict) else None
        if not pending_turn:
            return {}
        pending_turn_id = str(pending_turn.get("turn_id") or "")
        pending_prompt = str(pending_turn.get("prompt") or "")
        if (
            turn_id
            and pending_turn_id
            and pending_turn_id != str(turn_id)
            and (not prompt or pending_prompt != str(prompt))
        ):
            return {}
        watchdog = pending_turn.get("watchdog") if isinstance(pending_turn.get("watchdog"), dict) else {}
        pid = int(watchdog.get("pid") or 0) if str(watchdog.get("pid") or "").isdigit() else 0
        completed = dict(pending_turn)
        if summary:
            completed["summary"] = summary
        completed["completed_at"] = finished_at
        meta["last_completed_turn"] = completed
        meta.pop("pending_turn", None)
        save_meta(session, meta)
    touch_session_event(session, source="turn-complete")
    if pid > 0 and process_is_alive(pid):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
    return completed


def session_watch_status(session: str) -> Dict[str, Any]:
    meta = load_meta(session)
    pending_turn = meta.get("pending_turn") if isinstance(meta.get("pending_turn"), dict) else {}
    watchdog = pending_turn.get("watchdog") if isinstance(pending_turn.get("watchdog"), dict) else {}
    pid = int(watchdog.get("pid") or 0) if str(watchdog.get("pid") or "").isdigit() else 0
    last_progress_at = float(watchdog.get("last_progress_at") or 0.0)
    return {
        "session": session,
        "active": bool(pending_turn),
        "turn_id": str(pending_turn.get("turn_id") or ""),
        "submitted_at": float(pending_turn.get("submitted_at") or 0.0),
        "watchdog_pid": pid,
        "watchdog_alive": process_is_alive(pid),
        "watchdog_state": str(watchdog.get("state") or ""),
        "watchdog_started_at": float(watchdog.get("started_at") or 0.0),
        "watchdog_last_progress_at": last_progress_at,
        "watchdog_last_sample_at": float(watchdog.get("last_sample_at") or 0.0),
        "watchdog_idle_seconds": max(0.0, time.time() - last_progress_at) if last_progress_at else 0.0,
        "watchdog_stop_requested": bool(watchdog.get("stop_requested") or False),
        "watchdog_last_event": str(watchdog.get("last_event") or ""),
        "watchdog_last_signature": str(watchdog.get("last_signature") or ""),
        "notifications": dict(pending_turn.get("notifications") or {}),
    }


def run_session_watchdog(
    session: str,
    *,
    turn_id: str,
    poll_interval: float = WATCHDOG_POLL_INTERVAL,
    stalled_after: float = WATCHDOG_STALLED_AFTER,
    needs_input_after: float = WATCHDOG_NEEDS_INPUT_AFTER,
    reminder_after: float = WATCHDOG_REMINDER_AFTER,
    notify_buffer: float = WATCHDOG_NOTIFY_BUFFER,
) -> str:
    while True:
        meta = load_meta(session)
        pending_turn = meta.get("pending_turn") if isinstance(meta.get("pending_turn"), dict) else None
        if not pending_turn or str(pending_turn.get("turn_id") or "") != turn_id:
            return "completed"
        plugin = get_agent(str(meta.get("agent") or "codex"))
        watchdog = pending_turn.get("watchdog") if isinstance(pending_turn.get("watchdog"), dict) else {}
        if bool(watchdog.get("stop_requested")):
            update_watchdog_metadata(
                session,
                turn_id=turn_id,
                values={"state": "stopped", "stopped_at": time.time()},
            )
            return "stopped"
        sample = sample_watchdog_state(session, pane_id=str(pending_turn.get("pane_id") or meta.get("pane_id") or ""))
        now = time.time()
        previous_signature = str(watchdog.get("last_signature") or "")
        previous_cursor = (
            str(watchdog.get("last_cursor_x") or ""),
            str(watchdog.get("last_cursor_y") or ""),
        )
        current_cursor = (str(sample.get("cursor_x") or ""), str(sample.get("cursor_y") or ""))
        progress_detected = observable_progress_detected(previous_signature, previous_cursor, sample)
        failure_summary = plugin.extract_failure_summary(
            str(sample.get("capture") or ""),
            str(pending_turn.get("prompt") or ""),
        )
        last_progress_at = _watchdog_time_value(
            watchdog.get("last_progress_at"),
            pending_turn.get("submitted_at"),
            default=now,
        )
        idle_samples = int(watchdog.get("idle_samples") or 0)
        state = "running"
        if failure_summary:
            state = "failed"
        elif progress_detected:
            last_progress_at = now
            idle_samples = 0
        else:
            idle_samples += 1
            idle_seconds = max(0.0, now - last_progress_at)
            if not bool(sample.get("agent_running")):
                state = "failed"
            elif idle_samples >= 2 and idle_seconds >= needs_input_after:
                state = "needs-input"
            elif idle_samples >= 2 and idle_seconds >= stalled_after:
                state = "stalled"
        reset_values: Dict[str, Any] = {}
        if state == "running":
            if (
                watchdog.get("pending_event")
                or watchdog.get("pending_event_at")
                or watchdog.get("pending_event_summary")
            ):
                reset_values.update(
                    {
                        "pending_event": "",
                        "pending_event_at": 0.0,
                        "pending_event_summary": "",
                    }
                )
            previous_event = str(watchdog.get("last_event") or "")
            if previous_event in {"stalled", "needs-input", "failed"}:
                release_turn_notification(session, previous_event, turn_id=turn_id)
                reset_values.update(
                    {
                        "last_event": "",
                        "last_event_at": 0.0,
                    }
                )
        update_watchdog_metadata(
            session,
            turn_id=turn_id,
            values={
                "pid": os.getpid(),
                "state": state,
                "last_signature": str(sample.get("signature") or ""),
                "last_cursor_x": current_cursor[0],
                "last_cursor_y": current_cursor[1],
                "last_cpu_percent": float(sample.get("cpu_percent") or 0.0),
                "last_sample_at": now,
                "last_progress_at": last_progress_at,
                "idle_samples": idle_samples,
                **reset_values,
            },
        )
        if state in {"failed", "stalled", "needs-input"}:
            emitted = False
            last_event = str(watchdog.get("last_event") or "")
            summary = failure_summary or _watchdog_summary_for_event(
                state,
                pending_turn=pending_turn,
                capture=str(sample.get("capture") or ""),
            )
            if last_event == state:
                if (
                    watchdog.get("pending_event")
                    or watchdog.get("pending_event_at")
                    or watchdog.get("pending_event_summary")
                ):
                    update_watchdog_metadata(
                        session,
                        turn_id=turn_id,
                        values={
                            "pending_event": "",
                            "pending_event_at": 0.0,
                            "pending_event_summary": "",
                        },
                    )
            else:
                ready, pending_values = _watchdog_pending_event_ready(
                    watchdog,
                    event=state,
                    summary=summary,
                    now=now,
                    notify_buffer=notify_buffer,
                )
                if not ready:
                    if pending_values:
                        update_watchdog_metadata(session, turn_id=turn_id, values=pending_values)
                elif str(load_meta(session).get("pending_turn", {}).get("watchdog", {}).get("last_event") or "") != state:
                    if pending_values:
                        update_watchdog_metadata(session, turn_id=turn_id, values=pending_values)
                    emitted = emit_internal_notify(
                        session,
                        event=state,
                        summary=summary,
                        status=_watchdog_event_status(state),
                        turn_id=turn_id,
                        cwd=str(meta.get("cwd") or ""),
                        source="watchdog",
                        tail_text=str(sample.get("capture") or ""),
                    )
            update_watchdog_metadata(
                session,
                turn_id=turn_id,
                values={
                    "last_event": state if emitted else last_event,
                    "last_event_at": now if emitted else float(watchdog.get("last_event_at") or 0.0),
                },
            )
            if state == "failed":
                return state
        latest_notify_at = _latest_notification_at(pending_turn or {})
        if (
            state in {"stalled", "needs-input"}
            and latest_notify_at > 0.0
            and now - latest_notify_at >= reminder_after
        ):
            reminder_bucket = int(now // max(reminder_after, 1.0))
            reminder_key = f"reminder:{state}:{int(latest_notify_at)}:{reminder_bucket}"
            emit_internal_notify(
                session,
                event="reminder",
                summary=_watchdog_reminder_summary(session, state),
                status=_watchdog_event_status(state),
                turn_id=turn_id,
                cwd=str(meta.get("cwd") or ""),
                source="watchdog",
                notification_key=reminder_key,
            )
        time.sleep(max(0.5, poll_interval))


def ensure_session(
    session: str,
    cwd: Path,
    agent: str,
    *,
    approve_all: bool = False,
    runtime_home: Optional[str] = None,
    codex_home: Optional[str] = None,
    notify_to: Optional[str] = None,
    notify_target: Optional[str] = None,
) -> str:
    cwd = cwd.resolve()
    plugin = get_agent(agent)
    existing_meta = load_meta(session)
    if existing_meta and session_launch_mode(existing_meta) != "managed":
        raise OrcheError(
            f"Session {session} is already bound to native open mode. "
            "Reuse it through orche open with the same raw agent args, or close it and recreate it."
        )
    existing_cwd = Path(str(existing_meta.get("cwd") or "")).resolve() if existing_meta.get("cwd") else None
    if existing_cwd is not None and existing_cwd != cwd:
        raise OrcheError(
            f"Session {session} is already bound to cwd={existing_cwd}. "
            "Use the same --cwd or close the session and create a new one."
        )
    existing_agent = str(existing_meta.get("agent") or "").strip()
    if existing_agent and existing_agent != plugin.name:
        raise OrcheError(
            f"Session {session} is already bound to agent={existing_agent}. "
            "Close the session and create a new one for a different agent."
        )
    existing_notify_binding = _read_notify_binding(existing_meta)
    provided_notify_to = str(notify_to or "").strip()
    provided_notify_target = str(notify_target or "").strip()
    if (not provided_notify_to or not provided_notify_target) and not existing_notify_binding:
        raise OrcheError("managed sessions require both notify_to and notify_target")
    provided_notify_binding = (
        build_notify_binding(provided_notify_to, provided_notify_target)
        if provided_notify_to and provided_notify_target
        else existing_notify_binding
    )
    if existing_meta and provided_notify_binding != existing_notify_binding:
        if existing_notify_binding:
            raise OrcheError(
                f"Session {session} is already bound to notify_to={existing_notify_binding['provider']} "
                f"notify_target={existing_notify_binding['target']}. "
                "Use the same notify binding or close the session and create a new one."
            )
    resolved_notify_binding = existing_notify_binding or provided_notify_binding
    resolved_discord_channel_id = (
        resolved_notify_binding.get("target")
        if resolved_notify_binding.get("provider") == "discord"
        else ""
    )

    requested_runtime_home = runtime_home or codex_home
    managed_runtime_home = False
    if requested_runtime_home:
        resolved_runtime_home = normalize_runtime_home(requested_runtime_home)
        runtime = AgentRuntime(home=resolved_runtime_home, managed=False, label=plugin.runtime_label)
    elif runtime_home_from_meta(existing_meta):
        resolved_runtime_home = runtime_home_from_meta(existing_meta)
        managed_runtime_home = runtime_home_managed_from_meta(existing_meta)
        runtime = AgentRuntime(
            home=resolved_runtime_home,
            managed=managed_runtime_home,
            label=runtime_label_from_meta(existing_meta, plugin),
        )
    else:
        runtime = prepare_managed_runtime(
            plugin,
            session,
            cwd=cwd,
            discord_channel_id=resolved_discord_channel_id,
        )
        resolved_runtime_home = normalize_runtime_home(runtime.home)
        managed_runtime_home = True
    if managed_runtime_home and existing_meta and runtime_home_from_meta(existing_meta):
        runtime = prepare_managed_runtime(
            plugin,
            session,
            cwd=cwd,
            discord_channel_id=resolved_discord_channel_id,
        )
        resolved_runtime_home = normalize_runtime_home(runtime.home)
    existing_runtime_home = runtime_home_from_meta(existing_meta)
    if existing_runtime_home and resolved_runtime_home and existing_runtime_home != resolved_runtime_home:
        raise OrcheError(
            f"Session {session} is already bound to runtime_home={existing_runtime_home}. "
            f"Use the same {plugin.runtime_option_name} or close the session and create a new one."
        )
    tmux_mode = str(existing_meta.get("tmux_mode") or "").strip()
    host_pane_id = str(existing_meta.get("host_pane_id") or "").strip()
    tmux_host_session = str(existing_meta.get("tmux_host_session") or "").strip()
    if not tmux_mode and resolved_notify_binding:
        use_inline_pane, tmux_host_session, host_pane_id = _should_use_inline_pane(resolved_notify_binding)
        tmux_mode = "inline-pane" if use_inline_pane else "dedicated-session"
    elif not tmux_mode:
        tmux_mode = "dedicated-session"
    parent_session = ""
    if tmux_mode == "inline-pane" and str(resolved_notify_binding.get("provider") or "").strip() == "tmux-bridge":
        parent_session = str(resolved_notify_binding.get("target") or "").strip()
    pane_id = ensure_pane(
        session,
        cwd,
        agent,
        tmux_mode=tmux_mode,
        host_pane_id=host_pane_id,
        tmux_host_session=tmux_host_session,
    )
    meta = load_meta(session)
    meta.update(
        {
            "backend": BACKEND,
            "session": session,
            "cwd": str(cwd),
            "agent": agent,
            "pane_id": pane_id,
            "launch_mode": "managed",
            "tmux_mode": tmux_mode,
            "host_pane_id": host_pane_id,
            "tmux_host_session": tmux_host_session,
            "last_seen_at": time.time(),
            "parent_session": parent_session,
            "last_event_at": time.time(),
            "last_event_source": "open",
            "expires_after_seconds": managed_session_ttl_seconds(),
        }
    )
    apply_runtime_to_meta(meta, agent=agent, runtime=runtime)
    meta.pop("native_cli_args", None)
    meta.pop("discord_channel_id", None)
    meta.pop("discord_session", None)
    meta.pop("notify_routes", None)
    if resolved_notify_binding:
        meta["notify_binding"] = resolved_notify_binding
    else:
        meta.pop("notify_binding", None)
    save_meta(session, meta)
    wait_for_startup = False
    if plugin.name in {"claude", "codex"} and runtime.managed:
        current_meta = load_meta(session)
        startup = current_meta.get("startup") if isinstance(current_meta.get("startup"), dict) else {}
        if is_agent_running(plugin, pane_id):
            wait_for_startup = _managed_startup_reuse_wait_policy(session, plugin, pane_id, startup)
        else:
            initialize_session_startup(session)
            wait_for_startup = True
    pane_id = ensure_agent_running(
        plugin,
        session,
        cwd,
        pane_id,
        approve_all=approve_all,
        runtime=runtime,
        discord_channel_id=resolved_discord_channel_id,
    )
    meta = load_meta(session)
    meta.update(
        {
            "backend": BACKEND,
            "session": session,
            "cwd": str(cwd),
            "agent": agent,
            "pane_id": pane_id,
            "launch_mode": "managed",
            "tmux_mode": tmux_mode,
            "host_pane_id": host_pane_id,
            "tmux_host_session": tmux_host_session,
            "last_seen_at": time.time(),
            "parent_session": parent_session,
            "last_event_at": time.time(),
            "last_event_source": "open",
            "expires_after_seconds": managed_session_ttl_seconds(),
        }
    )
    apply_runtime_to_meta(meta, agent=agent, runtime=runtime)
    meta.pop("native_cli_args", None)
    meta.pop("discord_channel_id", None)
    meta.pop("discord_session", None)
    meta.pop("notify_routes", None)
    if resolved_notify_binding:
        meta["notify_binding"] = resolved_notify_binding
    else:
        meta.pop("notify_binding", None)
    save_meta(session, meta)
    touch_session_event(session, source="open")
    if wait_for_startup:
        wait_for_managed_startup_ready(session, plugin, pane_id, cwd)
    elif plugin.name == "claude":
        wait_for_agent_ready(plugin, pane_id, cwd)
    update_runtime_config(
        session=session,
        cwd=cwd,
        agent=agent,
        pane_id=pane_id,
        tmux_session=str(meta.get("tmux_session") or ""),
        runtime_home=resolved_runtime_home,
        runtime_home_managed=managed_runtime_home,
        runtime_label=runtime.label,
    )
    return pane_id


def send_prompt(
    session: str,
    cwd: Path,
    agent: str,
    prompt: str,
    *,
    approve_all: bool = False,
    pane_id: str = "",
) -> str:
    plugin = get_agent(agent)
    resolved_pane_id = str(pane_id or "").strip()
    meta = load_meta(session)
    if not resolved_pane_id:
        if session_launch_mode(meta) == "native":
            resolved_pane_id = ensure_native_session(
                session,
                cwd,
                agent,
                cli_args=native_cli_args_from_meta(meta),
            )
        else:
            resolved_pane_id = ensure_session(
                session,
                cwd,
                agent,
                approve_all=approve_all,
            )
    meta = load_meta(session)
    wait_for_ack = plugin.name == "claude" and runtime_home_managed_from_meta(meta) and session_launch_mode(meta) != "native"
    before_capture = read_pane(resolved_pane_id, DEFAULT_CAPTURE_LINES)
    turn_id = uuid.uuid4().hex[:12]
    meta["pending_turn"] = {
        "turn_id": turn_id,
        "prompt": prompt,
        "before_capture": before_capture,
        "submitted_at": time.time(),
        "pane_id": resolved_pane_id,
        "notifications": {},
        "prompt_ack": {
            "state": "pending",
            "accepted_at": 0.0,
            "source": "",
        },
        "watchdog": {
            "state": "queued",
            "started_at": 0.0,
            "last_progress_at": time.time(),
            "last_sample_at": 0.0,
            "idle_samples": 0,
            "stop_requested": False,
        },
    }
    save_meta(session, meta)
    touch_session_event(session, source="prompt-submit")
    plugin.submit_prompt(session, prompt, bridge=BRIDGE)
    try:
        start_session_watchdog(session, turn_id=turn_id)
    except Exception as exc:  # pragma: no cover
        log_exception("watchdog.start.error", exc, session=session, turn_id=turn_id)
    append_action_history(session, cwd, agent, "prompt", prompt=prompt, pane_id=resolved_pane_id)
    if wait_for_ack:
        wait_for_prompt_ack(session, turn_id=turn_id, prompt=prompt)
    return resolved_pane_id


def latest_turn_summary(session: str) -> str:
    meta = load_meta(session)
    pending_turn = meta.get("pending_turn") if isinstance(meta.get("pending_turn"), dict) else None
    if pending_turn:
        agent = str(meta.get("agent") or "codex")
        plugin = get_agent(agent)
        prompt = str(pending_turn.get("prompt") or "")
        deadline = time.monotonic() + LATEST_TURN_SUMMARY_RETRY_SECONDS
        summary = ""
        while True:
            pane_id = str((bridge_resolve(session) or pending_turn.get("pane_id") or meta.get("pane_id") or "")).strip()
            capture = read_pane(pane_id, DEFAULT_CAPTURE_LINES) if pane_id else ""
            before_capture = str(pending_turn.get("before_capture") or "")
            summary = _completion_summary_from_capture(
                plugin,
                capture=capture,
                before_capture=before_capture,
                prompt=prompt,
            )
            if summary or time.monotonic() >= deadline:
                break
            time.sleep(LATEST_TURN_SUMMARY_RETRY_INTERVAL)
        if summary:
            complete_pending_turn(session, summary=summary)
            return summary
        save_meta(session, meta)
        return ""
    last_completed = meta.get("last_completed_turn") if isinstance(meta.get("last_completed_turn"), dict) else None
    if last_completed:
        return str(last_completed.get("summary") or "")
    return ""


def build_status(session: str) -> Dict[str, Any]:
    meta = load_meta(session)
    if not meta:
        raise OrcheError(f"Unknown session: {session}")
    pane_id = bridge_resolve(session) or str(meta.get("pane_id") or "")
    info = get_pane_info(pane_id) if pane_id else None
    resolved_tmux_session = str((info or {}).get("session_name") or meta.get("tmux_session") or "").strip()
    cwd = str(meta.get("cwd") or (info or {}).get("pane_current_path") or "-")
    agent = str(meta.get("agent") or "codex")
    plugin = get_agent(agent)
    notify_binding = _read_notify_binding(meta) if meta else {}
    discord_session = notify_binding.get("session", "") if notify_binding.get("provider") == "discord" else ""
    runtime_home = runtime_home_from_meta(meta)
    runtime_home_managed = runtime_home_managed_from_meta(meta)
    agent_running = bool(pane_id and is_agent_running(plugin, pane_id))
    pending_turn = meta.get("pending_turn") if isinstance(meta.get("pending_turn"), dict) else {}
    watchdog = pending_turn.get("watchdog") if isinstance(pending_turn.get("watchdog"), dict) else {}
    startup = meta.get("startup") if isinstance(meta.get("startup"), dict) else {}
    prompt_ack = pending_turn.get("prompt_ack") if isinstance(pending_turn.get("prompt_ack"), dict) else {}
    parent_session = session_parent(meta)
    child_count = len(session_children(session, live_only=True))
    ttl_seconds = int(meta.get("expires_after_seconds") or managed_session_ttl_seconds())
    last_event_at = managed_session_last_event_at(meta)
    return {
        "backend": BACKEND,
        "session": session,
        "cwd": cwd,
        "agent": agent,
        "runtime_home": runtime_home,
        "runtime_home_managed": runtime_home_managed,
        "runtime_label": runtime_label_from_meta(meta, plugin),
        "codex_home": str(meta.get("codex_home") or runtime_home),
        "codex_home_managed": bool(meta.get("codex_home_managed") or runtime_home_managed),
        "tmux_session": resolved_tmux_session or "-",
        "pane_id": pane_id or "-",
        "window_name": (info or {}).get("window_name", meta.get("window_name", "-")),
        "agent_running": agent_running,
        "codex_running": agent_running,
        "pane_exists": bool(pane_id and pane_exists(pane_id)),
        "discord_session": discord_session,
        "notify_binding": notify_binding,
        "parent_session": parent_session,
        "child_count": child_count,
        "last_event_at": last_event_at,
        "ttl_seconds": ttl_seconds,
        "ttl_exempt_because_parent_alive": bool(parent_session and _session_has_live_parent(meta)),
        "pending_turn_id": str(pending_turn.get("turn_id") or ""),
        "pending_turn_submitted_at": float(pending_turn.get("submitted_at") or 0.0),
        "startup": dict(startup),
        "prompt_ack": dict(prompt_ack),
        "watchdog": dict(watchdog),
    }


def resolve_session_context(
    *,
    session: str,
    require_existing: bool = False,
    require_cwd_agent: bool = False,
) -> Tuple[Optional[Path], Optional[str], Dict[str, Any]]:
    meta = load_meta(session)
    cwd = Path(meta["cwd"]).resolve() if meta.get("cwd") else None
    agent = str(meta.get("agent")) if meta.get("agent") else None
    if require_existing and not meta:
        raise OrcheError(f"Unknown session: {session}")
    if require_cwd_agent and (cwd is None or agent is None):
        raise OrcheError(f"Session {session} is missing cwd/agent context; open it first")
    return cwd, agent, meta


def current_session_id() -> str:
    env_session = str(os.environ.get("ORCHE_SESSION") or "").strip()
    if env_session:
        return env_session

    current_pane_id = _current_tmux_value("#{pane_id}")
    if current_pane_id:
        for entry in list_sessions():
            if str(entry.get("pane_id") or "").strip() == current_pane_id:
                session = str(entry.get("session") or "").strip()
                if session:
                    return session

    current_tmux_session = _current_tmux_value("#{session_name}")
    if current_tmux_session:
        for entry in list_sessions():
            session = str(entry.get("session") or "").strip()
            if not session:
                continue
            mapped_tmux_session = str(entry.get("tmux_session") or tmux_session_name(session)).strip()
            if mapped_tmux_session == current_tmux_session:
                return session

    pane_title = _current_tmux_value("#{pane_title}")
    if pane_title:
        meta = load_meta(pane_title)
        if meta:
            return str(meta.get("session") or pane_title).strip()

    raise OrcheError("Unable to resolve current orche session id. Set ORCHE_SESSION or run inside an orche tmux pane.")


def cancel_session(session: str) -> str:
    _cwd, agent, _meta = resolve_session_context(session=session)
    plugin = get_agent(agent or "codex")
    plugin.interrupt(session, bridge=BRIDGE)
    return bridge_resolve(session) or "-"


def _close_session_single(session: str) -> str:
    meta = load_meta(session)
    if not meta:
        return "-"
    agent = str(meta.get("agent") or "codex")
    plugin = get_agent(agent)
    pane_id = bridge_resolve(session) or str(meta.get("pane_id") or "")
    info = get_pane_info(pane_id) if pane_id and pane_exists(pane_id) else None
    tmux_mode = str(meta.get("tmux_mode") or "").strip() or "dedicated-session"
    target_tmux_session = str((info or {}).get("session_name") or meta.get("tmux_session") or "").strip()
    if not target_tmux_session:
        target_tmux_session = tmux_session_name(session)
    with contextlib.suppress(Exception):
        stop_session_watchdog(session)
    if tmux_mode == "inline-pane":
        if pane_id and pane_exists(pane_id):
            tmux("kill-pane", "-t", pane_id, check=False, capture=True)
    else:
        for client_tty in list_tmux_session_clients(target_tmux_session):
            tmux("detach-client", "-t", client_tty, check=False, capture=True)
        if _tmux_has_session(target_tmux_session):
            tmux("kill-session", "-t", target_tmux_session, check=False, capture=True)
    runtime_home = runtime_home_from_meta(meta)
    if runtime_home and runtime_home_managed_from_meta(meta):
        plugin.cleanup_runtime(
            AgentRuntime(
                home=runtime_home,
                managed=True,
                label=runtime_label_from_meta(meta, plugin),
            )
        )
    config = load_config()
    if str(config.get("session") or "") == session:
        config["session"] = ""
        config["cwd"] = ""
        config["agent"] = ""
        config["pane_id"] = ""
        config["runtime_home"] = ""
        config["runtime_home_managed"] = False
        config["runtime_label"] = ""
        config["codex_home"] = ""
        config["codex_home_managed"] = False
        config["tmux_session"] = ""
        config["updated_at"] = time.time()
        save_config(config)
    remove_meta(session)
    return pane_id or "-"


def close_session_tree(session: str, *, reason: str = "", _visited: Optional[Set[str]] = None) -> str:
    session_name = str(session or "").strip()
    if not session_name:
        return "-"
    visited = _visited if _visited is not None else set()
    if session_name in visited:
        return "-"
    visited.add(session_name)
    root_pane = bridge_resolve(session_name) or str(load_meta(session_name).get("pane_id") or "") or "-"
    for child in session_children(session_name):
        close_session_tree(child, reason=reason, _visited=visited)
    _close_session_single(session_name)
    return root_pane


def close_session(session: str) -> str:
    return close_session_tree(session)
