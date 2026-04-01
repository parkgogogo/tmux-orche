from __future__ import annotations

from orche.notify.config import DiscordNotifyConfig, NotifyConfig
from orche.notify.payload import build_message_from_payload, parse_payload, summarize_assistant_message


def test_parse_payload_rejects_invalid_json():
    assert parse_payload("") is None
    assert parse_payload("not-json") is None
    assert parse_payload('["not-a-mapping"]') is None


def test_summarize_assistant_message_strips_formatting():
    summary = summarize_assistant_message(
        "\n# Title\n**Standalone**\n- item one\n1. item two\n``\n```py\nprint('x')\n```",
        max_chars=200,
    )

    assert summary == "Title item one item two"


def test_build_message_from_payload_prefers_explicit_values():
    message = build_message_from_payload(
        '{"event":"turn-complete","last_agent_message":"## Done\\n- fixed it","cwd":"/repo","session":"payload-session"}',
        notify_config=NotifyConfig(),
        runtime_config={"discord_channel_id": "111", "session": "runtime-session", "cwd": "/runtime"},
        summary_loader=lambda session: "",
        explicit_channel_id="222",
        explicit_session="explicit-session",
    )

    assert message is not None
    assert message.channel_id == "222"
    assert message.session == "explicit-session"
    assert "Done fixed it" in message.content
    assert "cwd: `/repo`" in message.content
    assert "session: `explicit-session`" in message.content


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
    assert "[failure]" in message.content
    assert "Recovered summary" in message.content


def test_build_message_from_payload_skips_unsupported_event():
    message = build_message_from_payload(
        '{"event":"turn-started","summary":"ignore"}',
        notify_config=NotifyConfig(),
        runtime_config={"discord_channel_id": "111"},
        summary_loader=lambda session: "",
    )

    assert message is None


def test_build_message_from_payload_requires_channel():
    message = build_message_from_payload(
        '{"event":"turn-complete","summary":"done"}',
        notify_config=NotifyConfig(),
        runtime_config={},
        summary_loader=lambda session: "",
    )

    assert message is None


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
    assert message.content == "Nested summary"
    assert message.session == "nested-session"


def test_build_message_from_payload_uses_second_nested_event_source():
    message = build_message_from_payload(
        '{"notification":{"event":" "},"payload":{"event":"turn-complete","summary":"Nested summary"}}',
        notify_config=NotifyConfig(discord=DiscordNotifyConfig(mention_user_id="")),
        runtime_config={"discord_channel_id": "111"},
        summary_loader=lambda session: "",
    )

    assert message is not None
    assert message.content == "Nested summary"


def test_build_message_from_payload_uses_default_prefix_when_summary_is_blank():
    message = build_message_from_payload(
        '{"event":"turn-complete","summary":"   "}',
        notify_config=NotifyConfig(discord=DiscordNotifyConfig(mention_user_id="")),
        runtime_config={"discord_channel_id": "111"},
        summary_loader=lambda session: "",
    )

    assert message is not None
    assert message.content == "Codex turn complete"


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
