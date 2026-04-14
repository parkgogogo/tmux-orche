from __future__ import annotations

import re
import shlex
import time
from pathlib import Path

from .runtime import DEFAULT_CODEX_SOURCE_HOME, default_codex_home_path, default_notify_hook_path, materialize_managed_codex_home, rewrite_codex_config
from ..base import AgentPlugin, AgentRuntime, BridgeIO
from ..common import DEFAULT_RUNTIME_HOME_ROOT, ensure_orche_shim, normalize_runtime_home, remove_runtime_home


READY_SURFACE_HINTS = ("OpenAI Codex", "Approvals:", "model:", "full-auto", "dangerously-bypass-approvals-and-sandbox", "Esc to interrupt", "Ctrl-C to interrupt")
CODEX_SUBMIT_SETTLE_MIN_SECONDS = 0.5
CODEX_SUBMIT_SETTLE_MAX_SECONDS = 1.5
CODEX_SUBMIT_SECONDS_PER_CHAR = 0.01


def codex_submit_settle_seconds(prompt: str) -> float:
    if not prompt:
        return 0.0
    scaled = len(prompt) * CODEX_SUBMIT_SECONDS_PER_CHAR
    return max(CODEX_SUBMIT_SETTLE_MIN_SECONDS, min(CODEX_SUBMIT_SETTLE_MAX_SECONDS, scaled))


def _compact_prompt_text(value: str) -> str:
    return " ".join((value or "").split()).strip()


def _is_codex_status_line(line: str) -> bool:
    compact = _compact_prompt_text(line).lower()
    if not compact:
        return False
    if compact.startswith(("tip:", "command:", "chunk id:", "wall time:", "output:")):
        return True
    return "gpt-" in compact and "% left" in compact


def _is_codex_prompt_continuation(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith(("› ", "• ", "⚠ ", "╭", "╰", "│", "└")) and not _is_codex_status_line(stripped)


def _find_codex_prompt_block(lines: list[str], prompt: str) -> tuple[int, int] | None:
    prompt_inline = _compact_prompt_text(prompt)
    if not prompt_inline:
        return None
    for index, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped.startswith("› "):
            continue
        parts = [stripped[2:].strip()]
        end_index = index
        cursor = index + 1
        while cursor < len(lines) and _is_codex_prompt_continuation(lines[cursor]):
            parts.append(lines[cursor].strip())
            end_index = cursor
            cursor += 1
        rendered_prompt = _compact_prompt_text(" ".join(parts))
        if rendered_prompt and (rendered_prompt in prompt_inline or prompt_inline in rendered_prompt):
            return index, end_index
    return None


def _find_next_codex_prompt(lines: list[str], start_index: int) -> int | None:
    for index in range(max(start_index, 0), len(lines)):
        if lines[index].strip().startswith("› "):
            return index
    return None


def _is_codex_transient_output(line: str) -> bool:
    compact = _compact_prompt_text(line).lower()
    return not compact or "esc to interrupt" in compact or compact.startswith(("working ", "working("))


def _is_codex_output_continuation(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith(("› ", "• ", "⚠ ", "╭", "╰", "│", "└")) and not _is_codex_status_line(stripped)


def _extract_codex_completion_summary(capture: str, prompt: str) -> str:
    lines = capture.splitlines()
    prompt_block = _find_codex_prompt_block(lines, prompt)
    if prompt_block is None:
        return ""
    _prompt_start, prompt_end = prompt_block
    next_prompt_index = _find_next_codex_prompt(lines, prompt_end + 1)
    if next_prompt_index is None:
        return ""
    summaries: list[str] = []
    current_output: list[str] = []
    for raw_line in lines[prompt_end + 1 : next_prompt_index]:
        stripped = raw_line.strip()
        if not stripped:
            if current_output:
                summary = _compact_prompt_text(" ".join(current_output))
                if summary and not _is_codex_transient_output(summary):
                    summaries.append(summary)
                current_output = []
            continue
        if stripped.startswith("• "):
            if current_output:
                summary = _compact_prompt_text(" ".join(current_output))
                if summary and not _is_codex_transient_output(summary):
                    summaries.append(summary)
            current_output = [stripped[2:].strip()]
            continue
        if current_output and _is_codex_output_continuation(raw_line):
            current_output.append(stripped)
            continue
        if current_output:
            summary = _compact_prompt_text(" ".join(current_output))
            if summary and not _is_codex_transient_output(summary):
                summaries.append(summary)
            current_output = []
    if current_output:
        summary = _compact_prompt_text(" ".join(current_output))
        if summary and not _is_codex_transient_output(summary):
            summaries.append(summary)
    return summaries[-1] if summaries else ""


class CodexAgent(AgentPlugin):
    name = "codex"
    display_name = "Codex"
    runtime_label = "CODEX_HOME"
    login_prompts = ("Login with ChatGPT", "Please login")

    def ensure_managed_runtime(self, session: str, *, cwd: Path, discord_channel_id: str | None) -> AgentRuntime:
        target = default_codex_home_path(session)
        materialize_managed_codex_home(DEFAULT_CODEX_SOURCE_HOME, target)
        from ..common import write_notify_hook

        write_notify_hook(default_notify_hook_path(target))
        rewrite_codex_config(target, session=session, cwd=cwd, discord_channel_id=discord_channel_id)
        return AgentRuntime(home=str(target.resolve()), managed=True, label=self.runtime_label)

    def build_launch_command(self, *, cwd: Path, runtime: AgentRuntime, session: str, discord_channel_id: str | None, approve_all: bool) -> str:
        _ = approve_all
        prefix = [f"cd {shlex.quote(str(cwd))}"]
        orche_shim = ensure_orche_shim()
        prefix.append(f"export ORCHE_BIN={shlex.quote(str(orche_shim))}")
        prefix.append(f"export PATH={shlex.quote(str(orche_shim.parent))}:$PATH")
        normalized_runtime_home = normalize_runtime_home(runtime.home)
        if normalized_runtime_home:
            prefix.append(f"mkdir -p {shlex.quote(normalized_runtime_home)}")
            prefix.append(f"export CODEX_HOME={shlex.quote(normalized_runtime_home)}")
        if session:
            prefix.append(f"export ORCHE_SESSION={shlex.quote(session)}")
        if discord_channel_id:
            from ..common import validate_discord_channel_id

            prefix.append(f"export ORCHE_DISCORD_CHANNEL_ID={shlex.quote(validate_discord_channel_id(discord_channel_id))}")
        command = ["codex", "--enable", "codex_hooks", "--no-alt-screen", "-C", str(cwd), "--dangerously-bypass-approvals-and-sandbox"]
        prefix.append(f"exec {' '.join(shlex.quote(part) for part in command)}")
        return " && ".join(prefix)

    def native_launch_args(self, *, cwd: Path, cli_args: list[str] | tuple[str, ...]) -> list[str]:
        args = [str(value) for value in cli_args]
        command: list[str] = []
        if "--no-alt-screen" not in args:
            command.append("--no-alt-screen")
        if "-C" not in args:
            command.extend(["-C", str(cwd)])
        if "--dangerously-bypass-approvals-and-sandbox" not in args:
            command.append("--dangerously-bypass-approvals-and-sandbox")
        command.extend(args)
        return command

    def matches_process(self, pane_command: str, descendant_commands: list[str]) -> bool:
        if pane_command == "codex":
            return True
        return any("codex" in proc.lower() or "@openai/codex" in proc.lower() for proc in descendant_commands)

    def capture_has_ready_surface(self, capture: str, cwd: Path) -> bool:
        lowered = capture.lower()
        has_brand = "openai codex" in lowered or "\ncodex" in lowered or " codex" in lowered
        has_context = str(cwd) in capture or any(hint.lower() in lowered for hint in READY_SURFACE_HINTS)
        return has_brand and has_context

    def submit_prompt(self, session: str, prompt: str, *, bridge: BridgeIO) -> None:
        if prompt:
            bridge.type(session, prompt)
            time.sleep(codex_submit_settle_seconds(prompt))
        bridge.keys(session, ["Enter"])

    def extract_completion_summary(self, capture: str, prompt: str) -> str:
        return _extract_codex_completion_summary(capture, prompt)

    def cleanup_runtime(self, runtime: AgentRuntime) -> None:
        if runtime.home:
            remove_runtime_home(runtime.home)


PLUGINS = [CodexAgent()]
