from __future__ import annotations

import os
import subprocess
import time
import uuid
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from agents import get_agent_plugin
from tmux.client import tmux
from tmux.query import TMUX_SESSION, _tmux_has_session, get_pane_info, list_panes, pane_exists, read_pane

from .config import managed_session_ttl_seconds
from .meta import _iter_meta_payloads, load_meta, log_exception, remove_meta, save_meta, session_key, session_lock, target_session_io_lock


def tmux_session_name(session: str) -> str:
    return f"{TMUX_SESSION}-{session_key(session)}"


def session_launch_mode(meta: Mapping[str, Any]) -> str:
    return str(meta.get("launch_mode") or "").strip() or "managed"


def session_parent(meta: Mapping[str, Any]) -> str:
    return str(meta.get("parent_session") or "").strip()


def managed_session_last_event_at(meta: Mapping[str, Any], *, default: float = 0.0) -> float:
    for value in (meta.get("last_event_at"), meta.get("updated_at"), meta.get("last_seen_at")):
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
        return {"last_event_at": timestamp, "last_event_source": meta["last_event_source"], "expires_after_seconds": meta["expires_after_seconds"]}


def session_metadata_is_live(session: str, meta: Optional[Mapping[str, Any]] = None) -> bool:
    session_name = str(session or "").strip()
    payload: Mapping[str, Any] = meta or load_meta(session_name)
    if not session_name or not payload:
        return False
    pane_id = str(payload.get("pane_id") or "").strip()
    if pane_id and pane_exists(pane_id):
        return True
    resolved_pane_id = bridge_resolve(session_name)
    if resolved_pane_id and pane_exists(resolved_pane_id):
        return True
    if str(payload.get("tmux_mode") or "").strip() == "inline-pane":
        return False
    target_tmux_session = str(payload.get("tmux_session") or tmux_session_name(session_name)).strip()
    return bool(target_tmux_session and _tmux_has_session(target_tmux_session))


def session_children(session: str, *, live_only: bool = False) -> List[str]:
    target = str(session or "").strip()
    if not target:
        return []
    children: List[str] = []
    for payload in _iter_meta_payloads():
        child_session = str(payload.get("session") or "").strip()
        if not child_session or session_parent(payload) != target:
            continue
        if live_only and not session_metadata_is_live(child_session, payload):
            continue
        children.append(child_session)
    return sorted(dict.fromkeys(children))


def _session_has_live_parent(meta: Mapping[str, Any]) -> bool:
    parent = session_parent(meta)
    if not parent:
        return False
    parent_meta = load_meta(parent)
    return bool(parent_meta and session_metadata_is_live(parent, parent_meta))


def _managed_session_expires_at(meta: Mapping[str, Any]) -> float:
    ttl = int(meta.get("expires_after_seconds") or managed_session_ttl_seconds())
    last_event_at = managed_session_last_event_at(meta)
    return 0.0 if ttl <= 0 or last_event_at <= 0 else last_event_at + ttl


def list_sessions(*, expire_fn=None) -> List[Dict[str, Any]]:
    if expire_fn is not None:
        expire_fn()
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
    return bool(bridge_resolve(session_name) or _tmux_has_session(tmux_session_name(session_name)))


def expire_managed_sessions(*, now: Optional[float] = None, close_session_tree_fn=None) -> List[str]:
    timestamp = time.time() if now is None else now
    if managed_session_ttl_seconds() <= 0:
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
    if close_session_tree_fn is None:
        return sorted(dict.fromkeys(expired_roots))
    closed: List[str] = []
    for session in sorted(dict.fromkeys(expired_roots)):
        try:
            close_session_tree_fn(session, reason="ttl-expired")
        except Exception as exc:
            log_exception("managed_session.expire_close_failed", exc, session=session)
            continue
        closed.append(session)
    return closed


def _current_tmux_value(fmt: str) -> str:
    result = tmux("display-message", "-p", fmt, check=False, capture=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def _resolve_bridge_pane(session: str) -> str:
    session_name = str(session or "").strip()
    if not session_name:
        raise RuntimeError("session is required")
    for pane in list_panes():
        if str(pane.get("pane_title") or "").strip() == session_name:
            return str(pane.get("pane_id") or "").strip()
    meta_pane_id = str(load_meta(session_name).get("pane_id") or "").strip()
    if meta_pane_id and pane_exists(meta_pane_id):
        return meta_pane_id
    raise RuntimeError(f"Unknown session: {session_name}")


def tmux_bridge(*args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
    def bridge_result(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["tmux-bridge", *args], returncode, stdout=stdout, stderr=stderr)

    try:
        if not args:
            raise RuntimeError("tmux-bridge command is required")
        command = args[0]
        if command == "name":
            pane_id, session = args[1], args[2]
            tmux("select-pane", "-t", pane_id, "-T", session, check=True, capture=True)
            result = bridge_result()
        elif command == "resolve":
            result = bridge_result(stdout=_resolve_bridge_pane(args[1]))
        elif command == "read":
            session, lines = args[1], max(int(args[2]), 1)
            result = bridge_result(stdout=read_pane(_resolve_bridge_pane(session), lines))
        elif command == "type":
            session, text = args[1], args[2]
            pane_id = _resolve_bridge_pane(session)
            buffer_name = f"orche-{uuid.uuid4().hex}"
            try:
                tmux("load-buffer", "-b", buffer_name, "-", check=True, capture=True, input_text=text)
                tmux("paste-buffer", "-t", pane_id, "-b", buffer_name, check=True, capture=True)
            finally:
                tmux("delete-buffer", "-b", buffer_name, check=False, capture=True)
            result = bridge_result()
        elif command == "keys":
            pane_id = _resolve_bridge_pane(args[1])
            tmux("send-keys", "-t", pane_id, *args[2:], check=True, capture=True)
            result = bridge_result()
        else:
            raise RuntimeError(f"Unsupported tmux-bridge command: {command}")
    except Exception as exc:
        result = bridge_result(returncode=1, stderr=str(exc))
        if check:
            raise subprocess.CalledProcessError(result.returncode, result.args, output=result.stdout, stderr=result.stderr) from exc
    return result if capture else bridge_result(returncode=result.returncode)


def bridge_name_pane(pane_id: str, session: str) -> None:
    tmux_bridge("name", pane_id, session, check=True, capture=True)


def bridge_resolve(session: str) -> Optional[str]:
    result = tmux_bridge("resolve", session, check=False, capture=True)
    return result.stdout.strip() or None if result.returncode == 0 else None


def bridge_read(session: str, lines: int = 200) -> str:
    return tmux_bridge("read", session, str(lines), check=True, capture=True).stdout.rstrip("\n")


def bridge_type(session: str, text: str) -> None:
    if text:
        tmux_bridge("read", session, "1", check=True, capture=True)
        tmux_bridge("type", session, text, check=True, capture=True)


def bridge_keys(session: str, keys: Union[Iterable[str], str]) -> None:
    values = [keys] if isinstance(keys, str) else list(keys)
    if values:
        tmux_bridge("read", session, "1", check=True, capture=True)
        tmux_bridge("keys", session, *values, check=True, capture=True)


class _BridgeAdapter:
    def type(self, session: str, text: str) -> None:
        bridge_type(session, text)
    def keys(self, session: str, keys: Sequence[str]) -> None:
        bridge_keys(session, list(keys))


BRIDGE = _BridgeAdapter()


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
        raise RuntimeError(f"Tmux session not found for session: {session}")
    if str(meta.get("tmux_mode") or "").strip() == "inline-pane" and os.environ.get("TMUX") and _current_tmux_value("#{session_name}") == target_tmux_session:
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


def deliver_notify_to_session(session: str, prompt: str) -> str:
    with target_session_io_lock(session.strip()):
        pane_id = bridge_resolve(session)
        if not pane_id:
            raise RuntimeError(f"notify target session not found: {session}")
        target_meta = load_meta(session)
        target_agent = str(target_meta.get("agent") or "").strip().lower() or "codex"
        get_agent_plugin(target_agent).submit_prompt(session, prompt, bridge=BRIDGE)
        return pane_id
