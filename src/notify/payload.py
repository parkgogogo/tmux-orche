from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .config import NotifyConfig
from .models import NotifyEvent

EVENT_ALIASES = {
    "": "completed",
    "agent-turn-complete": "completed",
    "turn-complete": "completed",
    "turn_complete": "completed",
    "task_complete": "completed",
    "task-complete": "completed",
    "stop": "completed",
    "subagentstop": "completed",
    "completed": "completed",
    "complete": "completed",
    "startup-blocked": "startup-blocked",
    "startup_blocked": "startup-blocked",
    "stalled": "stalled",
    "needs-input": "needs-input",
    "needs_input": "needs-input",
    "failed": "failed",
}
SUPPORTED_EVENTS = set(EVENT_ALIASES)


def _first_string(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _payload_value(payload: Mapping[str, Any], *path_options: tuple[str, ...]) -> str:
    for path in path_options:
        current: Any = payload
        found = True
        for part in path:
            if not isinstance(current, Mapping) or part not in current:
                found = False
                break
            current = current[part]
        if found:
            text = _first_string(current)
            if text:
                return text
    return ""


def parse_payload(payload_text: str) -> Optional[Mapping[str, Any]]:
    raw = payload_text.strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, Mapping) else None


def _compact_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _is_list_like(line: str) -> bool:
    return bool(re.match(r"^([-*+]\s+|\d+\.\s+|>\s+)", line))


def _truncate_discord_text(text: str, max_chars: int) -> str:
    value = text.strip()
    if len(value) <= max_chars:
        return value
    ellipsis = "…"
    if max_chars <= len(ellipsis):
        return ellipsis[:max_chars]
    truncated = value[: max_chars - len(ellipsis)].rstrip()
    if truncated.count("```") % 2 == 1:
        closing = "\n```"
        if max_chars <= len(ellipsis) + len(closing):
            return value[:max_chars].rstrip()
        truncated = value[: max_chars - len(ellipsis) - len(closing)].rstrip()
        if "\n" in truncated:
            truncated = truncated.rsplit("\n", 1)[0].rstrip() or truncated
        return f"{truncated}{ellipsis}{closing}"
    return f"{truncated}{ellipsis}"


def summarize_assistant_message(text: str, *, max_chars: int) -> str:
    blocks = []
    paragraph_lines = []
    list_lines = []
    in_code_block = False
    code_language = ""
    code_lines = []
    code_truncated = False

    def flush_paragraph() -> None:
        if paragraph_lines:
            blocks.append(" ".join(paragraph_lines).strip())
            paragraph_lines.clear()

    def flush_list() -> None:
        if list_lines:
            blocks.append("\n".join(list_lines).strip())
            list_lines.clear()

    def flush_code_block() -> None:
        nonlocal code_language, code_truncated
        if not code_lines and not code_truncated:
            code_language = ""
            return
        content_lines = list(code_lines)
        if code_truncated:
            content_lines.append("...")
        blocks.append(f"```{code_language}\n" + "\n".join(content_lines) + "\n```")
        code_lines.clear()
        code_language = ""
        code_truncated = False

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            flush_paragraph()
            flush_list()
            if in_code_block:
                flush_code_block()
            else:
                code_language = stripped[3:].strip()
                code_lines.clear()
                code_truncated = False
            in_code_block = not in_code_block
            continue
        if in_code_block:
            if len(code_lines) < 5:
                code_lines.append(line)
            else:
                code_truncated = True
            continue
        if not stripped:
            flush_paragraph()
            flush_list()
            continue
        if re.match(r"^#{1,6}\s+", stripped):
            flush_paragraph()
            flush_list()
            title = _compact_text(re.sub(r"^#{1,6}\s+", "", stripped))
            blocks.append(f"**{title}**")
            continue
        normalized = _compact_text(stripped)
        if _is_list_like(normalized):
            flush_paragraph()
            list_lines.append(normalized)
            continue
        flush_list()
        paragraph_lines.append(normalized)
    if in_code_block:
        flush_code_block()
    flush_paragraph()
    flush_list()
    return _truncate_discord_text("\n\n".join(block for block in blocks if block.strip()), max_chars)


def _event_name(payload: Mapping[str, Any]) -> str:
    raw = _first_string(
        payload.get("event"),
        payload.get("type"),
        payload.get("kind"),
        payload.get("hook_event_name"),
        payload.get("notification_type"),
        payload.get("name"),
        _payload_value(payload, ("notification", "event")),
        _payload_value(payload, ("payload", "event")),
        _payload_value(payload, ("payload", "hook_event_name")),
    ).lower()
    return EVENT_ALIASES.get(raw, raw)


def _assistant_message(payload: Mapping[str, Any]) -> str:
    return _first_string(
        payload.get("last_agent_message"),
        payload.get("lastAgentMessage"),
        payload.get("last-assistant-message"),
        payload.get("last_assistant_message"),
        payload.get("lastAssistantMessage"),
        payload.get("summary"),
        _payload_value(payload, ("payload", "last_agent_message")),
        _payload_value(payload, ("payload", "lastAgentMessage")),
        _payload_value(payload, ("payload", "last-assistant-message")),
        _payload_value(payload, ("payload", "last_assistant_message")),
        _payload_value(payload, ("payload", "lastAssistantMessage")),
        _payload_value(payload, ("payload", "summary")),
        payload.get("content"),
        payload.get("body"),
        _payload_value(payload, ("payload", "content")),
        _payload_value(payload, ("payload", "body")),
        payload.get("message"),
        _payload_value(payload, ("payload", "message")),
    )


def _payload_transcript_path(payload: Mapping[str, Any]) -> str:
    return _first_string(
        payload.get("transcript_path"),
        payload.get("transcriptPath"),
        _payload_value(payload, ("payload", "transcript_path")),
        _payload_value(payload, ("payload", "transcriptPath")),
    )


def _assistant_message_from_transcript(payload: Mapping[str, Any], *, wait_seconds: float = 0.0) -> str:
    transcript_path = _payload_transcript_path(payload)
    if not transcript_path:
        return ""
    path = Path(transcript_path).expanduser()
    if not path.exists():
        return ""
    deadline = time.monotonic() + max(wait_seconds, 0.0)
    while True:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return ""
        for line in reversed(lines):
            raw = line.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, Mapping) or entry.get("type") != "assistant":
                continue
            message = entry.get("message")
            if not isinstance(message, Mapping):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for item in reversed(content):
                if not isinstance(item, Mapping):
                    continue
                if str(item.get("type") or "") != "text":
                    continue
                text = _first_string(item.get("text"))
                if text:
                    return text
        if time.monotonic() >= deadline:
            return ""
        time.sleep(0.25)
    return ""


def _payload_session(payload: Mapping[str, Any]) -> str:
    return _first_string(
        payload.get("session"),
        payload.get("session_id"),
        payload.get("sessionId"),
        payload.get("thread_id"),
        payload.get("threadId"),
        _payload_value(payload, ("payload", "session")),
        _payload_value(payload, ("payload", "session_id")),
        _payload_value(payload, ("payload", "sessionId")),
        _payload_value(payload, ("payload", "thread_id")),
        _payload_value(payload, ("payload", "threadId")),
    )


def _payload_cwd(payload: Mapping[str, Any]) -> str:
    return _first_string(payload.get("cwd"), _payload_value(payload, ("payload", "cwd")))


def _payload_turn_id(payload: Mapping[str, Any]) -> str:
    return _first_string(
        payload.get("turn_id"),
        payload.get("turnId"),
        _payload_value(payload, ("metadata", "turn_id")),
        _payload_value(payload, ("metadata", "turnId")),
        _payload_value(payload, ("payload", "turn_id")),
        _payload_value(payload, ("payload", "turnId")),
    )


def _payload_source(payload: Mapping[str, Any]) -> str:
    return _first_string(
        payload.get("source"),
        _payload_value(payload, ("metadata", "source")),
        _payload_value(payload, ("payload", "source")),
    )


def _payload_tail_text(payload: Mapping[str, Any]) -> str:
    return _first_string(
        payload.get("tail_text"),
        payload.get("tail"),
        _payload_value(payload, ("metadata", "tail_text")),
        _payload_value(payload, ("metadata", "tail")),
        _payload_value(payload, ("payload", "tail_text")),
        _payload_value(payload, ("payload", "tail")),
    )


def _payload_tail_lines(payload: Mapping[str, Any]) -> str:
    return _first_string(
        payload.get("tail_lines"),
        _payload_value(payload, ("metadata", "tail_lines")),
        _payload_value(payload, ("payload", "tail_lines")),
    )


def _default_summary_for_event(event_name: str, notify_config: NotifyConfig) -> str:
    if event_name == "failed":
        return "Agent turn failed"
    if event_name == "startup-blocked":
        return "Agent startup blocked"
    if event_name == "needs-input":
        return "Agent likely needs input"
    if event_name == "stalled":
        return "Agent turn stalled"
    return notify_config.default_message_prefix


def build_message_from_payload(
    payload_text: str,
    *,
    notify_config: NotifyConfig,
    runtime_config: Mapping[str, Any],
    summary_loader: Callable[[str], str],
    explicit_session: str = "",
    status: str = "success",
) -> Optional[NotifyEvent]:
    payload = parse_payload(payload_text)
    if payload is None:
        return None
    event_name = _event_name(payload)
    if event_name not in SUPPORTED_EVENTS:
        return None
    session = _first_string(explicit_session, _payload_session(payload), runtime_config.get("session"))
    cwd = _first_string(_payload_cwd(payload), runtime_config.get("cwd"))
    assistant_message = _assistant_message(payload)
    loaded_summary = summary_loader(session) if session else ""
    transcript_summary = (
        _assistant_message_from_transcript(payload, wait_seconds=3.0) if event_name == "completed" else ""
    )
    if event_name == "completed":
        assistant_message = _first_string(transcript_summary, loaded_summary, assistant_message)
    elif not assistant_message and loaded_summary:
        assistant_message = loaded_summary
    assistant_summary = summarize_assistant_message(
        assistant_message,
        max_chars=notify_config.summary_max_chars,
    )
    summary = assistant_summary or _default_summary_for_event(event_name, notify_config)
    normalized_status = status.strip().lower() or "success"
    return NotifyEvent(
        event=event_name or "completed",
        summary=summary,
        session=session,
        cwd=cwd,
        status=normalized_status,
        metadata={
            "turn_id": _payload_turn_id(payload),
            "source": _payload_source(payload),
            "tail_text": _payload_tail_text(payload),
            "tail_lines": _payload_tail_lines(payload),
        },
    )
