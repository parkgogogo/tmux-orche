# Configuration

```bash
orche config list
orche config get claude.command
orche config get claude.home-path
orche config set claude.command /opt/tools/claude-wrapper
orche config reset claude.command
orche config set claude.home-path ~/custom/.claude
orche config set claude.config-path ~/custom/claude.json
orche config set discord.bot-token "$BOT_TOKEN"
orche config set discord.mention-user-id 123456789012345678
orche config set discord.webhook-url "$WEBHOOK_URL"
orche config set managed.ttl-seconds 1800
orche config set notify.enabled true
```

`orche config get/set/reset/list` reads and writes the same JSON config file. You can update values through the CLI or edit the file directly.

## Config File Location

```text
~/.config/orche/config.json
```

If `XDG_CONFIG_HOME` is set, `orche` uses:

```text
$XDG_CONFIG_HOME/orche/config.json
```

## State Directory

```text
~/.local/share/orche/
```

If `XDG_DATA_HOME` is set, `orche` uses:

```text
$XDG_DATA_HOME/orche/
```

## Supported User Config Keys

- `claude.command`
  Override the Claude CLI command that `orche` launches. Default is `claude`.

- `claude.home-path`
  Override the Claude source home directory mirrored into managed runtimes. Default is `~/.claude`.

- `claude.config-path`
  Override the Claude source config path used for trust sync. Default is `~/.claude.json`.

- `discord.bot-token`
  Set the Discord bot token used for bot-token delivery.

- `discord.mention-user-id`
  Set the Discord user id to mention in delivered notifications.

- `discord.webhook-url`
  Set the Discord webhook URL used for webhook delivery.

- `managed.ttl-seconds`
  Set the managed-session idle TTL in seconds. Default is `43200`; `<= 0` disables TTL expiry.

- `notify.enabled`
  Enable or disable notify delivery globally.

## Notes

- `config.json` may also contain session or runtime fields written by `orche` itself.
- Those internal fields are not part of the stable hand-edited config surface.
- Prefer the keys above for user-managed configuration.

## Claude Custom Config

Use these when your Claude installation is wrapped or its source home/config is not in the default location.

Set a custom Claude executable:

```bash
orche config set claude.command /opt/tools/claude-wrapper
```

Set a custom Claude source home path:

```bash
orche config set claude.home-path ~/custom/.claude
```

Set a custom Claude source config path for trust sync:

```bash
orche config set claude.config-path ~/custom/claude.json
```

Reset one of these keys back to its default:

```bash
orche config reset claude.command
```

What each key changes:

- `claude.command` changes the binary or wrapper command that `orche` executes when it launches Claude.
- `claude.home-path` changes which Claude home directory `orche` mirrors into managed Claude runtimes.
- `claude.config-path` changes which Claude config file `orche` reads when it syncs trust settings into a managed worker runtime.

Typical cases:

- your system command is not literally named `claude`
- you use a wrapper script such as `/opt/tools/claude-wrapper`
- your Claude home directory is not `~/.claude`
- your Claude config lives somewhere other than `~/.claude.json`

Example `config.json`:

```json
{
  "claude_command": "/opt/tools/claude-wrapper",
  "claude_home_path": "/Users/you/custom/.claude",
  "claude_config_path": "/Users/you/custom/claude.json"
}
```

## Managed Codex Runtime Notes

Managed Codex runtimes keep using an isolated `CODEX_HOME`; `orche` also writes `check_for_update_on_startup = false` and `[notice].hide_rate_limit_model_nudge = true` there to avoid startup update checks and model-switch nudges interfering with session startup.
