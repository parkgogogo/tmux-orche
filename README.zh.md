# tmux-orche

[English](README.md)

面向持久化 CLI 会话的现代化 tmux 后端 Codex 编排工具。

`tmux-orche` 将一次性的 `orch.py` 脚本演进为可安装的 Python CLI，提供清晰的命令界面、基于 XDG 的配置方式，以及可复用的 tmux + `tmux-bridge` 后端，用于长期运行的 Codex 编排会话。

## 特性

- 基于 Typer + Rich 构建，可直接安装并使用 `orche` 命令
- 使用持久化 tmux 会话，而不是一次性的子进程执行
- 支持 fire-and-forget 的 prompt 提交，并可在之后继续检查同一会话
- 使用符合 XDG 规范的配置与状态路径
- 兼容现有 tmux + `tmux-bridge` 编排模型
- 可通过 XDG 配置 `~/.config/orche/config.json` 与 Codex 原生 notify hook 配合工作

## 安装

### 使用 pip 安装

从 PyPI 安装：

```bash
pip install tmux-orche
```

从本地仓库安装：

```bash
pip install .
```

### 使用 uv 安装

作为工具安装：

```bash
uv tool install tmux-orche
```

从本地仓库安装：

```bash
uv tool install .
```

如果你更希望安装到项目环境，而不是全局工具环境：

```bash
uv pip install .
```

### 从源码安装

```bash
git clone https://github.com/parkgogogo/orche
cd orche
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install .
```

安装完成后，`orche` 命令应当已经可通过 `PATH` 直接使用。

## 快速开始

创建或复用一个持久化 Codex 会话：

```bash
orche session-new --cwd /path/to/repo --agent codex
```

创建一个命名会话，并为 Codex 原生 notify hook 绑定 Discord 频道：

```bash
orche session-new \
  --cwd /path/to/repo \
  --agent codex \
  --name repo-codex-main \
  --discord-channel-id 123456789012345678
```

向已有会话发送 prompt：

```bash
orche prompt --session repo-codex-main --prompt "analyze the test failures"
```

查看会话状态：

```bash
orche status --session repo-codex-main
```

读取最近的终端输出：

```bash
orche read --session repo-codex-main --lines 80
```

输入文本但不按 Enter 提交：

```bash
orche type --session repo-codex-main --text "focus on database queries first"
```

发送按键：

```bash
orche keys --session repo-codex-main --key Enter
orche keys --session repo-codex-main --key Escape --key Enter
```

获取最新的 turn 摘要：

```bash
orche turn-summary --session repo-codex-main
```

中断当前运行任务：

```bash
orche cancel --session repo-codex-main
```

关闭会话窗口：

```bash
orche close --session repo-codex-main
```

## 命令

| Command | Description | Key Options |
| --- | --- | --- |
| `orche backend` | 打印当前后端类型。 | None |
| `orche config get` | 读取支持的运行时配置值。 | `<key>` |
| `orche config set` | 写入支持的运行时配置值。 | `<key>`, `<value>` |
| `orche config list` | 列出支持的运行时配置值。 | None |
| `orche session-new` | 创建或复用一个持久化 Codex 会话。 | `--cwd`, `--agent`, `--name`, `--discord-channel-id` |
| `orche prompt` | 向已有会话发送 fire-and-forget prompt。 | `--session`, `--prompt` |
| `orche status` | 显示 pane、cwd、运行状态和会话元数据。 | `--session` |
| `orche read` | 通过 `tmux-bridge` 读取最近的 pane 输出。 | `--session`, `--lines` |
| `orche type` | 向会话输入文本但不提交。 | `--session`, `--text` |
| `orche keys` | 向会话发送一个或多个按键。 | `--session`, `--key` |
| `orche cancel` | 向当前会话发送 `Ctrl-C`。 | `--session` |
| `orche turn-summary` | 打印某个会话最新推断出的 turn 摘要。 | `--session` |
| `orche close` | 关闭 tmux 窗口并移除本地会话元数据。 | `--session` |

查看内置帮助：

```bash
orche --help
orche session-new --help
orche prompt --help
```

## 配置

`tmux-orche` 遵循 XDG Base Directory 规范。

主配置文件：

```text
~/.config/orche/config.json
```

状态目录：

```text
~/.local/share/orche/
```

常见状态文件包括：

- `meta/<session>.json`：每个会话的元数据
- `history/<session>.jsonl`：本地动作历史
- `locks/<session>.lock`：会话协调锁
- `logs/orche.log`：运行时事件日志

运行时配置会保存如下字段：

- 当前活动 `session`
- 已解析的 `discord_session`
- `codex_turn_complete_channel_id`
- 当前 `cwd`、`agent` 和 `pane_id`

通知 hook 和辅助脚本应读取 `~/.config/orche/config.json`，或直接使用 `orche config get ...`。

你也可以直接通过 CLI 管理通知相关配置：

```bash
orche config set discord.bot-token "YOUR_DISCORD_BOT_TOKEN"
orche config set discord.channel-id "123456789012345678"
orche config set discord.webhook-url "https://discord.com/api/webhooks/..."
orche config set notify.enabled true
orche config list
```

支持的配置键：

- `discord.bot-token`
- `discord.channel-id`
- `discord.webhook-url`
- `notify.enabled`

## 依赖要求

`orche` 依赖以下外部工具，并要求它们已安装且可执行：

- `tmux`
- `tmux-bridge`
- `codex` CLI

Python 要求：

- Python `3.9+`

后端默认假设：

- tmux 可以创建并管理持久化窗口
- `tmux-bridge` 可以解析、读取、输入和发送按键到 pane
- `codex` 可以在 tmux 中以 `--no-alt-screen` 方式启动

## 后端模型

整体工作流有意保持简洁：

1. `session-new` 创建或复用一个命名的 tmux 后端 Codex 会话。
2. `prompt` 将工作提交到已有会话并立即返回。
3. `status` 和 `read` 在之后检查实时会话状态。
4. `type`、`keys`、`cancel` 和 `close` 用于继续控制同一个会话。

这样可以保持编排的有状态特性，而不要求 CLI 本身持续附着在会话上。

## 通知工作流

`tmux-orche` 的设计目标是与现有的 Codex 原生 notify 管线配合工作。仓库中还包含一个本地 hook 变体 [`scripts/notify-discord.sh`](./scripts/notify-discord.sh)，它基于成熟的 `discord-turn-notify.sh` 设计改造而来，但不会修改 `~/.codex/hooks/` 下的原始脚本。

### 架构

```text
Codex native notify
    |
    v
discord-turn-notify.sh
    |
    +--> reads ~/.config/orche/config.json written by orche
    |
    +--> reads Codex JSON payload
    |
    +--> calls orche turn-summary when it needs a concise summary
    |
    v
Discord
```

### `orche` 提供的能力

- `orche session-new` 会把活动会话上下文写入：
  - `~/.config/orche/config.json`
- `orche turn-summary --session <name>` 以 CLI 形式暴露当前的 turn 摘要逻辑
- `orche _turn-summary --session <name>` 也保留为隐藏兼容别名
- `orche config get/set/list` 为通知密钥和频道设置提供稳定接口

这让整体设计保持简洁：

- Codex 负责发出 notify 事件
- 现有 shell hook 负责 Discord 投递
- `orche` 仅负责提供会话元数据和摘要提取能力

### Codex 原生 Notify 配置

在 `~/.codex/config.toml` 中：

```toml
notify = ["/bin/bash", "/Users/dnq/.codex/hooks/discord-turn-notify.sh"]
```

这样 hook 就可以读取 `~/.config/orche/config.json`、解析 Codex payload，并向 Discord 发送消息。

如果你想使用仓库内适配过的 hook，可以把 Codex 指向：

```toml
notify = ["/bin/bash", "/path/to/orche/scripts/notify-discord.sh"]
```

### Codex Notify Setup

`orche` 有意不内置自动配置。

原因如下：

- `~/.codex/config.toml` 是用户级全局配置，而不是仓库内状态
- 用户可能已经有自己的 `notify` 管线，不应被覆盖
- Discord bot token 属于敏感信息，不应被静默写入脚本或配置文件

推荐方式是显式手动配置。

1. 确保 `~/.codex/config.toml` 已存在。

2. 添加 notify hook 配置：

```toml
notify = ["/bin/bash", "/Users/dnq/.codex/hooks/discord-turn-notify.sh"]
```

3. 如果 `~/.codex/hooks/discord-turn-notify.sh` 尚不存在，则创建它。

4. 在该 hook 中：
   - 读取 `~/.config/orche/config.json`，获取 `codex_turn_complete_channel_id`、`session` 和 `cwd`
   - 读取 Codex 传入的 JSON payload
   - 在需要精简完成摘要时，调用 `orche turn-summary --session "$session"`

5. 安全地提供密钥：
   - 优先使用 `orche config set discord.bot-token ...` 或 `DISCORD_BOT_TOKEN`
   - 避免在被版本控制跟踪的文件中硬编码 token
   - 使用 `orche config set` 或 `orche session-new --discord-channel-id ...` 设置 `discord.channel-id`

示例 shell 用法：

```bash
orche config set discord.bot-token "your-token"
orche config set discord.channel-id "123456789012345678"
orche session-new \
  --cwd /path/to/repo \
  --agent codex \
  --name repo-codex-main \
  --discord-channel-id 123456789012345678
```

然后在 hook 中：

```bash
session="$(jq -r '.session // ""' ~/.config/orche/config.json)"
summary="$(orche turn-summary --session "$session" 2>/dev/null || true)"
channel_id="$(orche config get discord.channel-id)"
bot_token="${DISCORD_BOT_TOKEN:-$(orche config get discord.bot-token)}"
```

这样职责边界会非常清晰：

- Codex 负责发出 notify 事件
- hook 负责对外投递
- `orche` 负责提供会话元数据和摘要提取能力

### 使用 `orche` 配合现有 Hook

1. 启动或复用一个会话：

```bash
orche session-new \
  --cwd /path/to/repo \
  --agent codex \
  --name repo-codex-main \
  --discord-channel-id 123456789012345678
```

2. 发送任务：

```bash
orche prompt --session repo-codex-main --prompt "review the latest changes"
```

3. 当 Codex 发出原生 notify 事件时，hook 会从 `~/.config/orche/config.json` 读取：

- `codex_turn_complete_channel_id`
- `session`
- `cwd`
- `agent`
- `pane_id`

4. 如果 hook 需要简短的完成摘要，应调用：

```bash
orche turn-summary --session repo-codex-main
```

### Hook 集成说明

如果你当前的 hook 仍然调用旧的 `orch.py _turn-summary`，请将该调用改为：

```bash
orche turn-summary --session "$session"
```

如果你希望尽量减少行为变化，也可以使用：

```bash
orche _turn-summary --session "$session"
```

通知链路的其余设计可以保持不变。

## License

MIT
