from __future__ import annotations

import contextlib
import json
import os
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Iterable, List

from json_utils import JSONInputTooLargeError, MAX_JSON_INPUT_BYTES, loads_json, read_json_file
from paths import ensure_directories, history_dir, locks_dir, meta_dir, orch_log_path
from text_utils import (
    _is_prompt_fragment,
    compact_text,
    default_session_name,
    extract_summary_candidate,
    longest_common_prefix,
    repo_name,
    session_key,
    shorten,
    slugify,
    turn_delta,
    window_name,
)


DEFAULT_MAX_INLINE_SESSIONS = 4
DEFAULT_MANAGED_SESSION_TTL_SECONDS = 43200


def log_event(event: str, **fields: Any) -> None:
    ensure_directories()
    record = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "pid": os.getpid(), "event": event, **fields}
    try:
        with orch_log_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def log_exception(event: str, exc: BaseException, **fields: Any) -> None:
    log_event(event, error_type=type(exc).__name__, error=str(exc), traceback=traceback.format_exc(), **fields)


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


def remove_meta(session: str) -> None:
    path = meta_path(session)
    if path.exists():
        path.unlink()


def append_history_entry(session: str, entry: Dict[str, Any]) -> None:
    ensure_directories()
    with history_path(session).open("a", encoding="utf-8") as fh:
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
                raise RuntimeError(error_message)
            time.sleep(0.1)
    try:
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


@contextlib.contextmanager
def session_lock(session: str, *, timeout: float = 5.0):
    with _path_lock(lock_path(session), timeout=timeout, error_message=f"Timed out waiting for session lock: {session}"):
        yield


@contextlib.contextmanager
def target_session_io_lock(session: str, *, timeout: float = 5.0):
    with _path_lock(notify_target_lock_path(session), timeout=timeout, error_message=f"Timed out waiting for target session IO lock: {session}"):
        yield


@contextlib.contextmanager
def inline_host_lock(tmux_session: str, host_pane_id: str = "", *, timeout: float = 5.0):
    with _path_lock(inline_host_lock_path(tmux_session, host_pane_id), timeout=timeout, error_message=f"Timed out waiting for inline host lock: {tmux_session}:{host_pane_id}"):
        yield
