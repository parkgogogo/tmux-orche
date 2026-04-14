from __future__ import annotations

import subprocess
import uuid
from typing import Iterable, Optional, Sequence, Union

from tmux.client import tmux
from tmux.query import list_panes, pane_exists, read_pane


def _resolve_bridge_pane(session: str, fallback_pane_id: str = "") -> str:
    session_name = str(session or "").strip()
    if not session_name:
        raise RuntimeError("session is required")
    for pane in list_panes():
        if str(pane.get("pane_title") or "").strip() == session_name:
            return str(pane.get("pane_id") or "").strip()
    resolved_fallback_pane_id = str(fallback_pane_id or "").strip()
    if resolved_fallback_pane_id and pane_exists(resolved_fallback_pane_id):
        return resolved_fallback_pane_id
    raise RuntimeError(f"Unknown session: {session_name}")


def tmux_bridge(
    *args: str,
    check: bool = True,
    capture: bool = True,
    fallback_pane_id: str = "",
) -> subprocess.CompletedProcess[str]:
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
            result = bridge_result(stdout=_resolve_bridge_pane(args[1], fallback_pane_id))
        elif command == "read":
            session, lines = args[1], max(int(args[2]), 1)
            result = bridge_result(stdout=read_pane(_resolve_bridge_pane(session, fallback_pane_id), lines))
        elif command == "type":
            session, text = args[1], args[2]
            pane_id = _resolve_bridge_pane(session, fallback_pane_id)
            buffer_name = f"orche-{uuid.uuid4().hex}"
            try:
                tmux("load-buffer", "-b", buffer_name, "-", check=True, capture=True, input_text=text)
                tmux("paste-buffer", "-t", pane_id, "-b", buffer_name, check=True, capture=True)
            finally:
                tmux("delete-buffer", "-b", buffer_name, check=False, capture=True)
            result = bridge_result()
        elif command == "keys":
            pane_id = _resolve_bridge_pane(args[1], fallback_pane_id)
            tmux("send-keys", "-t", pane_id, *args[2:], check=True, capture=True)
            result = bridge_result()
        else:
            raise RuntimeError(f"Unsupported tmux-bridge command: {command}")
    except Exception as exc:
        result = bridge_result(returncode=1, stderr=str(exc))
        if check:
            raise subprocess.CalledProcessError(
                result.returncode,
                result.args,
                output=result.stdout,
                stderr=result.stderr,
            ) from exc
    return result if capture else bridge_result(returncode=result.returncode)


def bridge_name_pane(pane_id: str, session: str) -> None:
    tmux_bridge("name", pane_id, session, check=True, capture=True)


def bridge_resolve(session: str, *, fallback_pane_id: str = "") -> Optional[str]:
    result = tmux_bridge("resolve", session, check=False, capture=True, fallback_pane_id=fallback_pane_id)
    return result.stdout.strip() or None if result.returncode == 0 else None


def bridge_read(session: str, lines: int = 200, *, fallback_pane_id: str = "") -> str:
    return tmux_bridge("read", session, str(lines), check=True, capture=True, fallback_pane_id=fallback_pane_id).stdout.rstrip("\n")


def bridge_type(session: str, text: str, *, fallback_pane_id: str = "") -> None:
    if text:
        tmux_bridge("read", session, "1", check=True, capture=True, fallback_pane_id=fallback_pane_id)
        tmux_bridge("type", session, text, check=True, capture=True, fallback_pane_id=fallback_pane_id)


def bridge_keys(session: str, keys: Union[Iterable[str], str], *, fallback_pane_id: str = "") -> None:
    values = [keys] if isinstance(keys, str) else list(keys)
    if values:
        tmux_bridge("read", session, "1", check=True, capture=True, fallback_pane_id=fallback_pane_id)
        tmux_bridge("keys", session, *values, check=True, capture=True, fallback_pane_id=fallback_pane_id)


class _BridgeAdapter:
    def type(self, session: str, text: str) -> None:
        bridge_type(session, text)

    def keys(self, session: str, keys: Sequence[str]) -> None:
        bridge_keys(session, list(keys))


BRIDGE = _BridgeAdapter()
