#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG_PATH="${HOME}/config/orch.json"
DEFAULT_ORCHE_BIN="orche"
if [ -x "${REPO_ROOT}/.venv/bin/orche" ]; then
  DEFAULT_ORCHE_BIN="${REPO_ROOT}/.venv/bin/orche"
fi
ORCHE_BIN="${ORCHE_BIN:-${DEFAULT_ORCHE_BIN}}"
MENTION_USER_ID="${MENTION_USER_ID:-1475734550813605959}"
DEFAULT_MESSAGE_PREFIX="Codex turn complete"
INCLUDE_CWD="true"
INCLUDE_SESSION_ID="true"
MAX_MESSAGE_CHARS=1500
SUMMARY_MAX_CHARS=1200

config_get() {
  local key="${1:-}"
  [ -n "${key}" ] || return 0
  "${ORCHE_BIN}" config get "${key}" 2>/dev/null || true
}

summarize_assistant_message() {
  local raw="${1:-}"
  [ -n "${raw}" ] || return 0

  printf '%s\n' "${raw}" \
    | awk '
        {
          gsub(/\r/, "", $0)
          line = $0
          sub(/^[[:space:]]+/, "", line)
          sub(/[[:space:]]+$/, "", line)
          if (line == "" || line ~ /^```/) next

          if (line ~ /^#{1,6}[[:space:]]+/) sub(/^#{1,6}[[:space:]]+/, "", line)
          if (line ~ /^\*\*[^*]+\*\*$/) next
          if (line ~ /^[-*][[:space:]]+/) sub(/^[-*][[:space:]]+/, "", line)
          if (line ~ /^[0-9]+\.[[:space:]]+/) sub(/^[0-9]+\.[[:space:]]+/, "", line)

          gsub(/`/, "", line)
          gsub(/[[:space:]]+/, " ", line)
          if (line == "") next

          lines[++count] = line
        }
        END {
          for (i = 1; i <= count; i++) {
            if (i > 1) printf " "
            printf "%s", lines[i]
          }
        }
      ' \
    | jq -Rr --argjson max "${SUMMARY_MAX_CHARS}" '.[0:$max]'
}

summarize_session_output() {
  local session="${1:-}"
  [ -n "${session}" ] || return 0

  local raw=""
  raw="$(
    "${ORCHE_BIN}" read --session "${session}" --lines 120 2>/dev/null || true
  )"
  [ -n "${raw}" ] || return 0

  printf '%s\n' "${raw}" \
    | awk '
        BEGIN { count = 0 }
        {
          gsub(/\r/, "", $0)
          line = $0
          sub(/^[[:space:]]+/, "", line)
          sub(/[[:space:]]+$/, "", line)

          if (line == "") next
          if (line ~ /^```/) next
          if (line ~ /^╭/) next
          if (line ~ /^╰/) next
          if (line ~ /^│/) next
          if (line ~ /^[─━]{6,}$/) next
          if (line ~ /^[[:punct:]─━]{20,}$/) next
          if (line ~ /^Tip:/) next
          if (line ~ /^>/) next
          if (line ~ /^› /) next
          if (line ~ /^• /) next
          if (line ~ /^Explored($| )/) next
          if (line ~ /^Ran($| )/) next
          if (line ~ /^List($| )/) next
          if (line ~ /^Read($| )/) next
          if (line ~ /^Updated Plan$/) next
          if (line ~ /^Edited /) next
          if (line ~ /^Command:/) next
          if (line ~ /^Chunk ID:/) next
          if (line ~ /^Wall time:/) next
          if (line ~ /^Process exited/) next
          if (line ~ /^Original token count:/) next
          if (line ~ /^Output:$/) next
          if (line ~ /^└ /) next
          if (line ~ /^│ /) next
          if (line ~ /OpenAI Codex/) next
          if (line ~ /gpt-[0-9]/) next
          if (line ~ /% left/) next
          if (line ~ /^session:/) next
          if (line ~ /^cwd:/) next
          if (line ~ /^dnq@.* % /) next
          if (line ~ /^\^C$/) next

          if (line ~ /^#{1,6}[[:space:]]+/) sub(/^#{1,6}[[:space:]]+/, "", line)
          if (line ~ /^[-*][[:space:]]+/) sub(/^[-*][[:space:]]+/, "", line)
          if (line ~ /^[0-9]+\.[[:space:]]+/) sub(/^[0-9]+\.[[:space:]]+/, "", line)

          gsub(/`/, "", line)
          gsub(/[[:space:]]+/, " ", line)
          if (line == "") next

          lines[++count] = line
        }
        END {
          for (i = count; i >= 1; i--) {
            if (lines[i] == "") continue
            out = lines[i]
            break
          }
          printf "%s", out
        }
      ' \
    | jq -Rr --argjson max "${SUMMARY_MAX_CHARS}" '.[0:$max]'
}

summarize_turn_output() {
  local session="${1:-}"
  [ -n "${session}" ] || return 0
  "${ORCHE_BIN}" _turn-summary --session "${session}" 2>/dev/null || true
}

if [ -n "${1:-}" ]; then
  payload="${1}"
else
  payload="$(cat || true)"
fi
[ -n "${payload}" ] || exit 0

if ! jq -e . >/dev/null 2>&1 <<<"${payload}"; then
  exit 0
fi

notify_enabled="$(config_get "notify.enabled")"
if [ "${notify_enabled}" = "false" ]; then
  exit 0
fi

event_name="$(
  jq -r '
    .event
    // .type
    // .kind
    // .notification_type
    // .name
    // .notification.event
    // .payload.event
    // ""
  ' <<<"${payload}"
)"

case "${event_name}" in
  ""|agent-turn-complete|turn-complete|turn_complete|task_complete|task-complete)
    ;;
  *)
    exit 0
    ;;
esac

channel_id="$(config_get "discord.channel-id")"
[ -n "${channel_id}" ] || exit 0

bot_token="${DISCORD_BOT_TOKEN:-$(config_get "discord.bot-token")}"
[ -n "${bot_token}" ] || exit 0

assistant_message="$(
  jq -r '
    .last_agent_message
    // .lastAgentMessage
    // .["last-agent-message"]
    // .last_assistant_message
    // .lastAssistantMessage
    // .["last-assistant-message"]
    // .summary
    // .payload.last_agent_message
    // .payload.lastAgentMessage
    // .payload["last-agent-message"]
    // .payload.last_assistant_message
    // .payload.lastAssistantMessage
    // .payload["last-assistant-message"]
    // .payload.summary
    // .content
    // .body
    // .payload.content
    // .payload.body
    // .message
    // .payload.message
    // ""
  ' <<<"${payload}"
)"

cwd_value="$(
  jq -r '
    .cwd
    // .payload.cwd
    // ""
  ' <<<"${payload}"
)"
if [ -z "${cwd_value}" ] && [ -f "${CONFIG_PATH}" ]; then
  cwd_value="$(
    jq -r '
      .cwd
      // ""
    ' <"${CONFIG_PATH}"
  )"
fi

session_id="$(
  jq -r '
    .session
    // ""
  ' <<<"${payload}"
)"
if [ -z "${session_id}" ] && [ -f "${CONFIG_PATH}" ]; then
  session_id="$(
    jq -r '
      .session
      // ""
    ' <"${CONFIG_PATH}"
  )"
fi
if [ -z "${session_id}" ]; then
  session_id="$(
    jq -r '
      .session_id
      // .sessionId
      // .thread_id
      // .threadId
      // .payload.session_id
      // .payload.sessionId
      // ""
    ' <<<"${payload}"
  )"
fi

if [ -z "${assistant_message}" ]; then
  assistant_message="$(summarize_turn_output "${session_id}")"
fi

if [ -z "${assistant_message}" ]; then
  assistant_message="$(summarize_session_output "${session_id}")"
fi

assistant_summary="$(summarize_assistant_message "${assistant_message}")"

case "${assistant_summary}" in
  ""|agent-turn-complete|turn-complete|turn_complete|task_complete|task-complete|Codex\ turn\ complete)
    message="${DEFAULT_MESSAGE_PREFIX}"
    ;;
  *)
    message="${assistant_summary}"
    ;;
esac

allowed_mentions='{"parse":[]}'
if [ -n "${MENTION_USER_ID}" ]; then
  message="<@${MENTION_USER_ID}> ${message}"
  allowed_mentions="$(jq -n --arg id "${MENTION_USER_ID}" '{parse: [], users: [$id]}')"
fi

if [ "${INCLUDE_CWD}" = "true" ] && [ -n "${cwd_value}" ]; then
  message="${message}"$'\n'"cwd: \`${cwd_value}\`"
fi

if [ "${INCLUDE_SESSION_ID}" = "true" ] && [ -n "${session_id}" ]; then
  message="${message}"$'\n'"session: \`${session_id}\`"
fi

message="$(jq -nr --arg s "${message}" --argjson max "${MAX_MESSAGE_CHARS}" '$s[:$max]')"

request_body="$(
  jq -n \
    --arg content "${message}" \
    --argjson allowed_mentions "${allowed_mentions}" \
    '{
      content: $content,
      allowed_mentions: $allowed_mentions
    }'
)"

curl \
  --silent \
  --show-error \
  --fail \
  --connect-timeout 3 \
  --max-time 8 \
  -X POST \
  -H "Authorization: Bot ${bot_token}" \
  -H "Content-Type: application/json" \
  -d "${request_body}" \
  "https://discord.com/api/v10/channels/${channel_id}/messages" \
  >/dev/null 2>&1 || true

exit 0
