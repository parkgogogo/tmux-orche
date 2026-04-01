from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping, Optional

from .config import NotifyConfig
from .models import Message

SUPPORTED_EVENTS = {
    "",
    "agent-turn-complete",
    "turn-complete",
    "turn_complete",
    "task_complete",
    "task-complete",
}


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
    return _first_string(
        payload.get("event"),
        payload.get("type"),
        payload.get("kind"),
        payload.get("notification_type"),
        payload.get("name"),
        _payload_value(payload, ("notification", "event")),
        _payload_value(payload, ("payload", "event")),
    )


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


def _channel_id(explicit_channel_id: str, runtime_config: Mapping[str, Any]) -> str:
    value = _first_string(
        explicit_channel_id,
        runtime_config.get("discord_channel_id"),
        runtime_config.get("codex_turn_complete_channel_id"),
    )
    return re.sub(r"\s+", "", value)


def build_message_from_payload(
    payload_text: str,
    *,
    notify_config: NotifyConfig,
    runtime_config: Mapping[str, Any],
    summary_loader: Callable[[str], str],
    explicit_channel_id: str = "",
    explicit_session: str = "",
    status: str = "success",
) -> Optional[Message]:
    payload = parse_payload(payload_text)
    if payload is None:
        return None
    if _event_name(payload) not in SUPPORTED_EVENTS:
        return None
    channel_id = _channel_id(explicit_channel_id, runtime_config)
    if not channel_id:
        return None
    session = _first_string(explicit_session, _payload_session(payload), runtime_config.get("session"))
    cwd = _first_string(_payload_cwd(payload), runtime_config.get("cwd"))
    assistant_message = _assistant_message(payload)
    if not assistant_message and session:
        assistant_message = summary_loader(session)
    assistant_summary = summarize_assistant_message(
        assistant_message,
        max_chars=notify_config.summary_max_chars,
    )
    content = assistant_summary or notify_config.default_message_prefix
    normalized_status = status.strip().lower() or "success"
    if normalized_status != "success":
        content = f"[{normalized_status}] {content}"
    mention_user_id = notify_config.discord.mention_user_id.strip()
    if mention_user_id:
        content = f"<@{mention_user_id}> {content}"
    if notify_config.include_cwd and cwd:
        content += f"\ncwd: `{cwd}`"
    if notify_config.include_session and session:
        content += f"\nsession: `{session}`"
    content = content[: notify_config.max_message_chars]
    return Message(
        content=content,
        channel_id=channel_id,
        session=session,
        status=normalized_status,
    )
