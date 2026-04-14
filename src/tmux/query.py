from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from session.meta import session_key, window_name

from .client import tmux


TMUX_SESSION = "orche"
LEGACY_TMUX_SESSION = "orche-smux"
DEFAULT_CAPTURE_LINES = 200
TMUX_PANE_OUTPUT_SEPARATOR = "@@ORCHE_PANE@@"


def _known_tmux_sessions() -> Tuple[str, ...]:
    return (TMUX_SESSION, LEGACY_TMUX_SESSION)


def _is_orche_tmux_session(name: str) -> bool:
    session_name = str(name or "").strip()
    return bool(session_name) and (session_name in _known_tmux_sessions() or session_name.startswith(f"{TMUX_SESSION}-"))


def _tmux_has_session(name: str) -> bool:
    session_name = str(name or "").strip()
    if not session_name:
        return False
    return tmux("has-session", "-t", session_name, check=False, capture=True).returncode == 0


def list_tmux_sessions() -> List[str]:
    result = tmux("list-sessions", "-F", "#{session_name}", check=False, capture=True)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if _is_orche_tmux_session(line.strip())]


def pane_exists(pane_id: str) -> bool:
    result = tmux("display-message", "-p", "-t", pane_id, "#{pane_id}", check=False, capture=True)
    return result.returncode == 0 and result.stdout.strip() == pane_id


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
    return parts if len(parts) == expected else []


def list_windows(target: Optional[str] = None) -> List[Dict[str, str]]:
    session_names = [target] if target else list_tmux_sessions()
    windows: List[Dict[str, str]] = []
    for session_name in session_names:
        result = tmux("list-windows", "-t", session_name, "-F", _tmux_join_fields("#{window_id}", "#{window_name}"), check=False, capture=True)
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
    result = tmux("list-windows", "-t", session_name, "-F", "#{window_index}", check=False, capture=True)
    if result.returncode != 0:
        return 0
    indexes = [int(line.strip()) for line in result.stdout.splitlines() if line.strip().isdigit()]
    return (max(indexes) + 1) if indexes else 0


def _tmux_window_index_in_use(exc: subprocess.CalledProcessError) -> bool:
    detail = (exc.stderr or exc.stdout or "").strip().lower()
    return "index " in detail and " in use" in detail


def list_panes(target: Optional[str] = None) -> List[Dict[str, str]]:
    args = ["list-panes", "-t", target] if target else ["list-panes", "-a"]
    args.extend(["-F", _tmux_join_fields("#{session_name}", "#{pane_id}", "#{window_id}", "#{window_name}", "#{pane_dead}", "#{pane_pid}", "#{pane_current_command}", "#{pane_current_path}", "#{pane_title}")])
    result = tmux(*args, check=False, capture=True)
    if result.returncode != 0:
        return []
    panes: List[Dict[str, str]] = []
    for line in result.stdout.splitlines():
        parts = _tmux_split_fields(line, expected=9)
        if len(parts) != 9 or (not target and not _is_orche_tmux_session(parts[0])):
            continue
        panes.append({"session_name": parts[0], "pane_id": parts[1], "window_id": parts[2], "window_name": parts[3], "pane_dead": parts[4], "pane_pid": parts[5], "pane_current_command": parts[6], "pane_current_path": parts[7], "pane_title": parts[8]})
    return panes


def _tmux_value_for_pane(pane_id: str, fmt: str) -> str:
    result = tmux("display-message", "-p", "-t", pane_id, fmt, check=False, capture=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def get_pane_info(pane_id: str) -> Optional[Dict[str, str]]:
    if not pane_exists(pane_id):
        return None
    raw = _tmux_value_for_pane(pane_id, _tmux_join_fields("#{session_name}", "#{pane_id}", "#{window_id}", "#{window_name}", "#{pane_dead}", "#{pane_pid}", "#{pane_current_command}", "#{pane_current_path}", "#{pane_title}"))
    parts = _tmux_split_fields(raw, expected=9)
    if len(parts) != 9:
        return None
    return {"session_name": parts[0], "pane_id": parts[1], "window_id": parts[2], "window_name": parts[3], "pane_dead": parts[4], "pane_pid": parts[5], "pane_current_command": parts[6], "pane_current_path": parts[7], "pane_title": parts[8]}


def read_pane(pane_id: str, lines: int = DEFAULT_CAPTURE_LINES) -> str:
    result = tmux("capture-pane", "-p", "-J", "-t", pane_id, "-S", f"-{max(lines, 1)}", check=False, capture=True)
    if result.returncode != 0:
        return ""
    return "\n".join(result.stdout.splitlines()[-lines:])


def pane_cursor_state(pane_id: str) -> Dict[str, str]:
    parts = _tmux_split_fields(_tmux_value_for_pane(pane_id, _tmux_join_fields("#{cursor_x}", "#{cursor_y}", "#{pane_in_mode}", "#{pane_dead}")), expected=4)
    while len(parts) < 4:
        parts.append("")
    return {"cursor_x": parts[0], "cursor_y": parts[1], "pane_in_mode": parts[2], "pane_dead": parts[3]}


def list_tmux_session_clients(session_name: str) -> List[str]:
    if not _tmux_has_session(session_name):
        return []
    result = tmux("list-clients", "-t", session_name, "-F", "#{client_tty}", check=False, capture=True)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def ensure_tmux_session(session: str, cwd: Path) -> str:
    name = f"{TMUX_SESSION}-{session_key(session)}"
    if _tmux_has_session(name):
        return name
    tmux("new-session", "-d", "-s", name, "-n", window_name(session), "-c", str(cwd), check=True, capture=True)
    if not _tmux_has_session(name):
        raise RuntimeError(f"Failed to create tmux session for {session}")
    return name
