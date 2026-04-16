from __future__ import annotations

import os
import time
from collections.abc import Mapping
from typing import Any, Dict, List, Optional

from tmux.bridge import bridge_resolve
from tmux.client import tmux
from tmux.query import TMUX_SESSION, _tmux_has_session, get_pane_info, pane_exists

from .config import managed_session_ttl_seconds
from .meta import (
    _iter_meta_payloads,
    load_meta,
    log_exception,
    remove_meta,
    save_meta,
    session_key,
    session_lock,
)


def tmux_session_name(session: str) -> str:
    return f"{TMUX_SESSION}-{session_key(session)}"


def session_parent(meta: Mapping[str, Any]) -> str:
    return str(meta.get("parent_session") or "").strip()


def session_last_event_at(meta: Mapping[str, Any], *, default: float = 0.0) -> float:
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
        if not meta:
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


def session_metadata_is_live(
    session: str, meta: Optional[Mapping[str, Any]] = None
) -> bool:
    session_name = str(session or "").strip()
    payload: Mapping[str, Any] = meta or load_meta(session_name)
    if not session_name or not payload:
        return False
    pane_id = str(payload.get("pane_id") or "").strip()
    if pane_id and pane_exists(pane_id):
        return True
    resolved_pane_id = bridge_resolve(session_name, fallback_pane_id=pane_id)
    if resolved_pane_id and pane_exists(resolved_pane_id):
        return True
    if str(payload.get("tmux_mode") or "").strip() == "inline-pane":
        return False
    target_tmux_session = str(
        payload.get("tmux_session") or tmux_session_name(session_name)
    ).strip()
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


def _session_expires_at(meta: Mapping[str, Any]) -> float:
    ttl = int(meta.get("expires_after_seconds") or managed_session_ttl_seconds())
    last_event_at = session_last_event_at(meta)
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
    fallback_pane_id = str(meta.get("pane_id") or "").strip() if meta else ""
    return bool(
        bridge_resolve(session_name, fallback_pane_id=fallback_pane_id)
        or _tmux_has_session(tmux_session_name(session_name))
    )


def expire_sessions(
    *, now: Optional[float] = None, close_session_tree_fn=None
) -> List[str]:
    timestamp = time.time() if now is None else now
    if managed_session_ttl_seconds() <= 0:
        return []
    expired_roots: List[str] = []
    for payload in _iter_meta_payloads():
        session = str(payload.get("session") or "").strip()
        if not session:
            continue
        if not session_metadata_is_live(session, payload):
            remove_meta(session)
            continue
        if _session_has_live_parent(payload):
            continue
        expires_at = _session_expires_at(payload)
        if expires_at > 0.0 and expires_at <= timestamp:
            expired_roots.append(session)
    if close_session_tree_fn is None:
        return sorted(dict.fromkeys(expired_roots))
    closed: List[str] = []
    for session in sorted(dict.fromkeys(expired_roots)):
        try:
            close_session_tree_fn(session, reason="ttl-expired")
        except Exception as exc:
            log_exception("session.expire_close_failed", exc, session=session)
            continue
        closed.append(session)
    return closed


def _current_tmux_value(fmt: str) -> str:
    result = tmux("display-message", "-p", fmt, check=False, capture=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def attach_session(session: str, *, pane_id: str = "") -> str:
    meta = load_meta(session)
    fallback_pane_id = str(meta.get("pane_id") or "").strip()
    resolved_pane_id = (
        pane_id
        or bridge_resolve(session, fallback_pane_id=fallback_pane_id)
        or fallback_pane_id
    )
    info = get_pane_info(resolved_pane_id) if resolved_pane_id else None
    target_tmux_session = str(meta.get("tmux_session") or "").strip()
    if info is not None:
        target_tmux_session = str(
            info.get("session_name") or target_tmux_session
        ).strip()
    if not target_tmux_session:
        target_tmux_session = tmux_session_name(session)
    if not _tmux_has_session(target_tmux_session):
        raise RuntimeError(f"Tmux session not found for session: {session}")
    if (
        str(meta.get("tmux_mode") or "").strip() == "inline-pane"
        and os.environ.get("TMUX")
        and _current_tmux_value("#{session_name}") == target_tmux_session
    ):
        target_window_id = str(
            (info or {}).get("window_id") or meta.get("window_id") or ""
        ).strip()
        if target_window_id:
            tmux("select-window", "-t", target_window_id, check=False, capture=True)
        if resolved_pane_id:
            tmux("select-pane", "-t", resolved_pane_id, check=False, capture=True)
        return target_tmux_session
    if os.environ.get("TMUX"):
        result = tmux(
            "switch-client", "-t", target_tmux_session, check=False, capture=True
        )
        if result.returncode != 0:
            tmux("attach-session", "-t", target_tmux_session, check=True, capture=False)
    else:
        tmux("attach-session", "-t", target_tmux_session, check=True, capture=False)
    return target_tmux_session
