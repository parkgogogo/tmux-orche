from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


class TmuxClientError(RuntimeError):
    pass


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
    raise TmuxClientError("tmux is not installed; orche requires tmux")


def tmux(
    *args: str,
    check: bool = True,
    capture: bool = True,
    input_text: Optional[str] = None,
) -> subprocess.CompletedProcess[str]:
    require_tmux()
    return run(["tmux", *args], check=check, capture=capture, input_text=input_text)


def process_cpu_percent(pid_text: str) -> float:
    pid = str(pid_text or "").strip()
    if not pid.isdigit():
        return 0.0
    result = run(["ps", "-o", "%cpu=", "-p", pid], check=False, capture=True)
    if result.returncode != 0:
        return 0.0
    try:
        return float((result.stdout or "").strip())
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
