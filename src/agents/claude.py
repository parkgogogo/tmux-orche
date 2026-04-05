from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

from .base import AgentPlugin, AgentRuntime
from .common import (
    DEFAULT_RUNTIME_HOME_ROOT,
    ensure_orche_shim,
    normalize_runtime_home,
    remove_runtime_home,
    session_key,
    validate_discord_channel_id,
    write_notify_hook,
    write_text_atomically,
)


READY_SURFACE_HINTS = (
    "Claude Code",
    "permission mode",
    "/help",
    "shift+tab",
    "esc to interrupt",
)


def _compact_prompt_text(value: str) -> str:
    return " ".join((value or "").split()).strip()


def _is_claude_separator(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and all(ch == "─" for ch in stripped)


def _find_claude_prompt_block(lines: list[str], prompt: str) -> tuple[int, int] | None:
    prompt_inline = _compact_prompt_text(prompt)
    if not prompt_inline:
        return None
    for index, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped.startswith("❯ "):
            continue
        parts = [stripped[2:].strip()]
        end_index = index
        cursor = index + 1
        while cursor < len(lines):
            continuation = lines[cursor].strip()
            if not continuation or continuation.startswith(("❯ ", "⏺ ", "⎿ ")):
                break
            if _is_claude_separator(continuation):
                break
            parts.append(continuation)
            end_index = cursor
            cursor += 1
        rendered_prompt = _compact_prompt_text(" ".join(parts))
        if rendered_prompt and (rendered_prompt in prompt_inline or prompt_inline in rendered_prompt):
            return index, end_index
    return None


def _extract_claude_completion_summary(capture: str, prompt: str) -> str:
    lines = capture.splitlines()
    prompt_block = _find_claude_prompt_block(lines, prompt)
    if prompt_block is None:
        return ""
    _prompt_start, prompt_end = prompt_block
    summaries: list[str] = []
    current_block: list[str] = []
    for raw_line in lines[prompt_end + 1 :]:
        stripped = raw_line.strip()
        if not stripped:
            if current_block:
                summaries.append(_compact_prompt_text(" ".join(current_block)))
                current_block = []
            continue
        if stripped.startswith("❯"):
            break
        if _is_claude_separator(stripped):
            continue
        if stripped.startswith("⎿ "):
            continue
        if stripped.startswith("⏺ "):
            if current_block:
                summaries.append(_compact_prompt_text(" ".join(current_block)))
            current_block = [stripped[2:].strip()]
            continue
        if not current_block:
            continue
        current_block.append(stripped)
    if current_block:
        summaries.append(_compact_prompt_text(" ".join(current_block)))
    cleaned = [summary for summary in summaries if summary and not re.match(r"^⏵⏵\s+", summary)]
    return cleaned[-1] if cleaned else ""


def default_claude_home_path(session: str) -> Path:
    return DEFAULT_RUNTIME_HOME_ROOT / f"orche-claude-{session_key(session)}"


def default_notify_hook_path(runtime_home: Path) -> Path:
    return runtime_home / "hooks" / "discord-turn-notify.sh"


def default_settings_path(runtime_home: Path) -> Path:
    return runtime_home / "settings.json"


def render_stop_hook_command(hook_path: Path, *, session: str, discord_channel_id: str | None) -> str:
    parts = ["/bin/bash", str(hook_path), "--session", session]
    if discord_channel_id:
        parts.extend(["--channel-id", validate_discord_channel_id(discord_channel_id)])
    return " ".join(shlex.quote(part) for part in parts)


def build_settings_payload(runtime_home: Path, *, session: str, discord_channel_id: str | None) -> dict[str, object]:
    return {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": render_stop_hook_command(
                                default_notify_hook_path(runtime_home),
                                session=session,
                                discord_channel_id=discord_channel_id,
                            ),
                        }
                    ]
                }
            ]
        }
    }

class ClaudeAgent(AgentPlugin):
    name = "claude"
    display_name = "Claude Code"
    runtime_label = "Claude settings"
    login_prompts = ("Please run /login", "Login required")

    def ensure_managed_runtime(
        self,
        session: str,
        *,
        cwd: Path,
        discord_channel_id: str | None,
    ) -> AgentRuntime:
        _ = cwd
        target = default_claude_home_path(session)
        target.mkdir(parents=True, exist_ok=True)
        write_notify_hook(default_notify_hook_path(target))
        settings_payload = build_settings_payload(
            target,
            session=session,
            discord_channel_id=discord_channel_id,
        )
        write_text_atomically(
            default_settings_path(target),
            json.dumps(settings_payload, indent=2, ensure_ascii=False) + "\n",
        )
        return AgentRuntime(home=str(target.resolve()), managed=True, label=self.runtime_label)

    def build_launch_command(
        self,
        *,
        cwd: Path,
        runtime: AgentRuntime,
        session: str,
        discord_channel_id: str | None,
        approve_all: bool,
    ) -> str:
        _ = approve_all
        prefix = [f"cd {shlex.quote(str(cwd))}"]
        orche_shim = ensure_orche_shim()
        prefix.append(f"export ORCHE_BIN={shlex.quote(str(orche_shim))}")
        prefix.append(f"export PATH={shlex.quote(str(orche_shim.parent))}:$PATH")
        if session:
            prefix.append(f"export ORCHE_SESSION={shlex.quote(session)}")
        if discord_channel_id:
            prefix.append(f"export ORCHE_DISCORD_CHANNEL_ID={shlex.quote(validate_discord_channel_id(discord_channel_id))}")
        settings_path = default_settings_path(Path(normalize_runtime_home(runtime.home)))
        command = ["claude", "--dangerously-skip-permissions", "--settings", str(settings_path)]
        prefix.append(f"exec {' '.join(shlex.quote(part) for part in command)}")
        return " && ".join(prefix)

    def matches_process(self, pane_command: str, descendant_commands: list[str]) -> bool:
        if pane_command in {"claude", "node"}:
            return True
        for proc in descendant_commands:
            lowered = proc.lower()
            if re_matches_claude(lowered):
                return True
        return False

    def capture_has_ready_surface(self, capture: str, cwd: Path) -> bool:
        lowered = capture.lower()
        has_brand = "claude code" in lowered or "\nclaude" in lowered or " claude" in lowered
        has_context = str(cwd) in capture or any(hint in lowered for hint in READY_SURFACE_HINTS)
        return has_brand and has_context

    def extract_completion_summary(self, capture: str, prompt: str) -> str:
        return _extract_claude_completion_summary(capture, prompt)

    def cleanup_runtime(self, runtime: AgentRuntime) -> None:
        if runtime.home:
            remove_runtime_home(runtime.home)


def re_matches_claude(command: str) -> bool:
    return "claude" in command or "claude-code" in command


PLUGINS = [ClaudeAgent()]
