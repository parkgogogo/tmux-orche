from __future__ import annotations

import json

from notify.config import DiscordNotifyConfig, NotifyConfig
from notify.payload import build_message_from_payload, parse_payload, summarize_assistant_message


def test_parse_payload_rejects_invalid_json():
    assert parse_payload("") is None
    assert parse_payload("not-json") is None
    assert parse_payload('["not-a-mapping"]') is None


def test_summarize_assistant_message_preserves_paragraphs_and_lists():
    summary = summarize_assistant_message(
        "First paragraph line 1  \nFirst paragraph line 2\n\n## Result\n- item one\n1. item two",
        max_chars=200,
    )

    assert summary == "First paragraph line 1 First paragraph line 2\n\n**Result**\n\n- item one\n1. item two"


def test_summarize_assistant_message_preserves_code_block_preview():
    summary = summarize_assistant_message(
        "Here is the fix:\n\n```py\nline1\nline2\nline3\nline4\nline5\nline6\n```\n\nDone.",
        max_chars=400,
    )

    assert summary == "Here is the fix:\n\n```py\nline1\nline2\nline3\nline4\nline5\n...\n```\n\nDone."


def test_summarize_assistant_message_closes_unfinished_code_block_when_truncated():
    summary = summarize_assistant_message(
        "```py\nline1\nline2\nline3\nline4\nline5\nline6\n```",
        max_chars=18,
    )

    assert summary.endswith("```")
    assert "…" in summary


def test_summarize_assistant_message_truncates_plain_text_for_discord():
    summary = summarize_assistant_message(
        "alpha beta gamma delta",
        max_chars=10,
    )

    assert summary == "alpha bet…"


def test_summarize_assistant_message_handles_tiny_discord_limit():
    summary = summarize_assistant_message(
        "alpha beta gamma",
        max_chars=1,
    )

    assert summary == "…"


def test_summarize_assistant_message_truncates_open_code_block_without_closing_when_limit_is_too_small():
    summary = summarize_assistant_message(
        "```py\nline1\nline2\nline3\nline4\nline5\nline6\n```",
        max_chars=4,
    )

    assert summary == "```p"


def test_summarize_assistant_message_truncates_open_code_block_before_first_newline():
    summary = summarize_assistant_message(
        "```py\nline1\nline2\nline3\nline4\nline5\nline6\n```",
        max_chars=6,
    )

    assert summary == "`…\n```"


def test_build_message_from_payload_prefers_explicit_values():
    message = build_message_from_payload(
        '{"event":"turn-complete","last_agent_message":"## Done\\n- fixed it","cwd":"/repo","session":"payload-session"}',
        notify_config=NotifyConfig(),
        runtime_config={"discord_channel_id": "111", "session": "runtime-session", "cwd": "/runtime"},
        summary_loader=lambda session: "",
        explicit_session="explicit-session",
    )

    assert message is not None
    assert message.event == "completed"
    assert message.session == "explicit-session"
    assert message.cwd == "/repo"
    assert message.summary == "**Done**\n\n- fixed it"


def test_build_message_from_payload_uses_summary_loader_and_failure_prefix():
    message = build_message_from_payload(
        '{"event":"turn-complete","session":"payload-session"}',
        notify_config=NotifyConfig(),
        runtime_config={"discord_channel_id": "111"},
        summary_loader=lambda session: "Recovered summary",
        status="failure",
    )

    assert message is not None
    assert message.status == "failure"
    assert message.summary == "Recovered summary"


def test_build_message_from_payload_skips_unsupported_event():
    message = build_message_from_payload(
        '{"event":"turn-started","summary":"ignore"}',
        notify_config=NotifyConfig(),
        runtime_config={"discord_channel_id": "111"},
        summary_loader=lambda session: "",
    )

    assert message is None


def test_build_message_from_payload_does_not_require_route_target():
    message = build_message_from_payload(
        '{"event":"turn-complete","summary":"done"}',
        notify_config=NotifyConfig(),
        runtime_config={},
        summary_loader=lambda session: "",
    )

    assert message is not None
    assert message.summary == "done"


def test_build_message_from_payload_reads_nested_payload_fields():
    message = build_message_from_payload(
        '{"payload":{"event":"turn-complete","summary":"Nested summary","cwd":"/nested","sessionId":"nested-session"}}',
        notify_config=NotifyConfig(
            discord=DiscordNotifyConfig(mention_user_id=""),
            include_cwd=False,
            include_session=False,
        ),
        runtime_config={"discord_channel_id": "111"},
        summary_loader=lambda session: "",
    )

    assert message is not None
    assert message.summary == "Nested summary"
    assert message.session == "nested-session"


def test_build_message_from_payload_uses_second_nested_event_source():
    message = build_message_from_payload(
        '{"notification":{"event":" "},"payload":{"event":"turn-complete","summary":"Nested summary"}}',
        notify_config=NotifyConfig(discord=DiscordNotifyConfig(mention_user_id="")),
        runtime_config={"discord_channel_id": "111"},
        summary_loader=lambda session: "",
    )

    assert message is not None
    assert message.summary == "Nested summary"


def test_build_message_from_payload_uses_default_prefix_when_summary_is_blank():
    message = build_message_from_payload(
        '{"event":"turn-complete","summary":"   "}',
        notify_config=NotifyConfig(discord=DiscordNotifyConfig(mention_user_id="")),
        runtime_config={"discord_channel_id": "111"},
        summary_loader=lambda session: "",
    )

    assert message is not None
    assert message.summary == "Agent turn complete"


def test_build_message_from_payload_accepts_claude_stop_hook_event():
    message = build_message_from_payload(
        '{"hook_event_name":"Stop","cwd":"/repo","session_id":"claude-session","summary":"Done"}',
        notify_config=NotifyConfig(discord=DiscordNotifyConfig(mention_user_id="")),
        runtime_config={"discord_channel_id": "111"},
        summary_loader=lambda session: "",
    )

    assert message is not None
    assert message.event == "completed"
    assert message.session == "claude-session"
    assert message.summary == "Done"


def test_build_message_from_payload_prefers_loaded_completed_summary_over_hook_payload():
    message = build_message_from_payload(
        '{"hook_event_name":"Stop","session_id":"claude-session","last_assistant_message":"· Hyperspacing…"}',
        notify_config=NotifyConfig(discord=DiscordNotifyConfig(mention_user_id="")),
        runtime_config={"discord_channel_id": "111"},
        summary_loader=lambda session: "CLAUDE_FINAL_TOKEN",
    )

    assert message is not None
    assert message.event == "completed"
    assert message.summary == "CLAUDE_FINAL_TOKEN"


def test_build_message_from_payload_prefers_transcript_text_for_completed_event(tmp_path):
    transcript_path = tmp_path / "claude.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {"type": "thinking", "thinking": "internal"},
                                {"type": "text", "text": "CLAUDE_TRANSCRIPT_TOKEN"},
                            ]
                        },
                    }
                )
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    message = build_message_from_payload(
        json.dumps(
            {
                "hook_event_name": "Stop",
                "session_id": "claude-session",
                "transcript_path": str(transcript_path),
                "last_assistant_message": "✻ Baking… (thinking)",
            }
        ),
        notify_config=NotifyConfig(discord=DiscordNotifyConfig(mention_user_id="")),
        runtime_config={"discord_channel_id": "111"},
        summary_loader=lambda session: "PANE_TOKEN",
    )

    assert message is not None
    assert message.summary == "CLAUDE_TRANSCRIPT_TOKEN"


def test_build_message_from_payload_waits_for_transcript_text_when_stop_hook_arrives_early(tmp_path, monkeypatch):
    transcript_path = tmp_path / "claude.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "internal"},
                    ]
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    now = {"value": 0.0}

    def fake_monotonic() -> float:
        return now["value"]

    def fake_sleep(seconds: float) -> None:
        transcript_path.write_text(
            transcript_path.read_text(encoding="utf-8")
            + json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "CLAUDE_DELAYED_TRANSCRIPT_TOKEN"},
                        ]
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        now["value"] += seconds

    monkeypatch.setattr("notify.payload.time.monotonic", fake_monotonic)
    monkeypatch.setattr("notify.payload.time.sleep", fake_sleep)

    message = build_message_from_payload(
        json.dumps(
            {
                "hook_event_name": "Stop",
                "session_id": "claude-session",
                "transcript_path": str(transcript_path),
                "last_assistant_message": "✻ Booping… (running stop hook · thinking)",
            }
        ),
        notify_config=NotifyConfig(discord=DiscordNotifyConfig(mention_user_id="")),
        runtime_config={"discord_channel_id": "111"},
        summary_loader=lambda session: "",
    )

    assert message is not None
    assert message.summary == "CLAUDE_DELAYED_TRANSCRIPT_TOKEN"


def test_build_message_from_payload_reads_watchdog_metadata_and_event_aliases():
    message = build_message_from_payload(
        '{"event":"needs_input","summary":"","session":"demo","metadata":{"turn_id":"turn-1","source":"watchdog"}}',
        notify_config=NotifyConfig(discord=DiscordNotifyConfig(mention_user_id="")),
        runtime_config={},
        summary_loader=lambda session: "",
        status="warning",
    )

    assert message is not None
    assert message.event == "needs-input"
    assert message.status == "warning"
    assert message.summary == "Agent likely needs input"
    assert message.metadata["turn_id"] == "turn-1"
    assert message.metadata["source"] == "watchdog"


def test_build_message_from_payload_reads_startup_blocked_tail_metadata():
    message = build_message_from_payload(
        '{"event":"startup_blocked","summary":"","session":"demo","metadata":{"source":"startup","tail_text":"line1\\nline2","tail_lines":20}}',
        notify_config=NotifyConfig(discord=DiscordNotifyConfig(mention_user_id="")),
        runtime_config={},
        summary_loader=lambda session: "",
        status="warning",
    )

    assert message is not None
    assert message.event == "startup-blocked"
    assert message.summary == "Agent startup blocked"
    assert message.metadata["source"] == "startup"
    assert message.metadata["tail_text"] == "line1\nline2"
    assert message.metadata["tail_lines"] == "20"


def test_build_message_from_payload_uses_failed_default_summary():
    message = build_message_from_payload(
        '{"event":"failed","summary":"   ","session":"demo"}',
        notify_config=NotifyConfig(discord=DiscordNotifyConfig(mention_user_id="")),
        runtime_config={},
        summary_loader=lambda session: "",
        status="failure",
    )

    assert message is not None
    assert message.summary == "Agent turn failed"


def test_build_message_from_payload_uses_stalled_default_summary():
    message = build_message_from_payload(
        '{"event":"stalled","summary":"   ","session":"demo"}',
        notify_config=NotifyConfig(discord=DiscordNotifyConfig(mention_user_id="")),
        runtime_config={},
        summary_loader=lambda session: "",
        status="warning",
    )

    assert message is not None
    assert message.summary == "Agent turn stalled"


def test_build_message_from_payload_returns_none_for_invalid_payload_text():
    assert (
        build_message_from_payload(
            "not-json",
            notify_config=NotifyConfig(),
            runtime_config={"discord_channel_id": "111"},
            summary_loader=lambda session: "",
        )
        is None
    )


def test_summarize_assistant_message_skips_empty_code_block():
    summary = summarize_assistant_message(
        "Before\n\n```py\n```\n\nAfter",
        max_chars=200,
    )

    assert summary == "Before\n\nAfter"


def test_summarize_assistant_message_keeps_short_code_block_and_unclosed_fence():
    summary = summarize_assistant_message(
        "## Heading\n\n```py\nprint('x')",
        max_chars=200,
    )

    assert summary == "**Heading**\n\n```py\nprint('x')\n```"
