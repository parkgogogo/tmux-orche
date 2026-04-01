# tmux-orche

[English](README.md)

面向 OpenClaw 与其他 fire-and-forget 工作流的 tmux 后端 Codex 编排工具。

## 概览

`tmux-orche` 主要解决一个非常实际的问题：OpenClaw 需要把任务交给 Codex 处理，但不希望一直挂在完整对话上等待 Codex 完成。

基本模式如下：

1. OpenClaw 接收到用户请求。
2. OpenClaw 调用 `orche` 创建或复用一个运行 Codex 的持久化 tmux 会话。
3. `orche` 立即返回。
4. OpenClaw 不再继续等待，因此不会在 Codex 工作期间持续消耗 token。
5. Codex 在后台 tmux 会话中继续工作。
6. 任务完成后，由 notify hook 将结果发回同一个 Discord 频道。

这就是 `tmux-orche` 的核心价值：让 OpenClaw 可以把长时间运行的 Codex 任务异步交出去，同时显著降低 OpenClaw 自身的 token 消耗。

## Codex 配置限制

Codex 不支持通过 CLI 参数直接指定任意配置文件路径。

实际行为是：

- `codex -c ...` 用于覆盖单个配置项
- 它并不表示“加载这个 config.toml 文件”
- Codex 仍然使用标准配置位置，例如 `~/.codex/config.toml` 或项目内的 `.codex/config.toml`

如果要支持多会话或多频道并发，实际可行的替代方案是 `CODEX_HOME`。

默认情况下，`tmux-orche` 会自动管理它：

- 在 `/tmp/orche-codex-<session>/` 创建目录
- 将 `~/.codex/` 的内容复制到该目录
- 为当前 session 和频道重写复制后的 `config.toml` 中的 notify 配置
- 使用该临时目录作为 `CODEX_HOME` 启动 Codex
- 在 session 关闭时删除该临时目录

`--codex-home` 仍然保留为高级覆盖项，但普通工作流不再需要它。

## 核心使用场景

推荐的生产工作流如下：

1. 用户在 Discord 服务器中发送任务，并 `@OpenClaw`。
2. OpenClaw 在主聊天频道中读取消息。
3. OpenClaw 调用 `orche session-new` 和 `orche send`。
4. `orche` 立即返回，OpenClaw 当前回合结束。
5. Codex 在后台持久化 tmux 会话中继续运行。
6. notify hook 会把完成消息发回同一个 Discord 频道。
7. 用户收到通知后，可以继续对话。

tmux 持久化在这里很关键：即使 OpenClaw 已经返回，Codex 进程仍会继续存活并运行。

## 前置条件

### 运行依赖

`orche` 依赖以下工具：

- `tmux`
- `tmux-bridge`
- `codex` CLI
- Python `3.9+`

### Discord 环境

核心的 OpenClaw + Codex 工作流默认假设你有一个 Discord 服务器，至少包含：

- 一个 Discord Guild
- 一个频道，例如 `#coding`，同时用于：
  - OpenClaw 接收用户消息
  - Codex 完成后将通知发回同一个频道

同时需要两个 Discord Bot：

- `OpenClaw Bot`：在该频道接收用户消息并调用 `orche`
- `Codex Notify Bot`：在 Codex 完成后把通知消息发回同一个频道

### OpenClaw 配置

典型的 OpenClaw 部署会在 `~/.openclaw/openclaw.json` 中启用 Discord。

相关字段通常包括：

- `channels.discord.enabled: true`
- `channels.discord.token`：OpenClaw Bot Token
- `channels.discord.guilds`：允许访问的 Guild 和 User 配置

示例：

```json
{
  "channels": {
    "discord": {
      "enabled": true,
      "token": "YOUR_OPENCLAW_BOT_TOKEN",
      "guilds": {
        "123456789012345678": {
          "enabled": true,
          "allowed_users": ["234567890123456789"]
        }
      }
    }
  }
}
```

## 架构

```text
Discord user
    |
    v
@OpenClaw in main channel (#coding)
    |
    v
OpenClaw Bot
    |
    +--> orche session-new
    |
    +--> orche send
    |
    v
OpenClaw returns immediately
    |
    v
Persistent tmux session
    |
    v
Codex runs in background
    |
    v
notify hook
    |
    v
Codex Notify Bot -> same Discord channel (#coding)
```

### 端到端流程

1. 用户通过 `@OpenClaw` 提交编码任务。
2. OpenClaw 验证 Discord 消息和允许的 guild/user 上下文。
3. OpenClaw 通过 `orche` 启动或复用一个 Codex tmux 会话。
4. OpenClaw 将任务发送给 Codex，并立即结束当前回合。
5. Codex 在 tmux 中异步工作。
6. notify hook 会把结果发回同一个频道。
7. 用户收到完成信号，而 OpenClaw 不需要一直保持整段会话开启。

## 安装

面向 agent 引导安装的完整说明：<https://github.com/parkgogogo/tmux-orche/raw/main/install.md>

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

创建一个命名会话，并为 notify hook 绑定 Discord 频道：

```bash
orche session-new \
  --cwd /path/to/repo \
  --agent codex \
  --name repo-codex-main \
  --discord-channel-id 123456789012345678
```

默认情况下，`orche` 会自动在 `/tmp/orche-codex-<session>/` 下创建并管理临时 `CODEX_HOME`。

向已有会话发送消息：

```bash
orche send --session repo-codex-main "analyze the test failures"
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
| `orche session-new` | 创建或复用一个持久化 Codex 会话。 | `--cwd`, `--agent`, `--name`, `--codex-home`, `--discord-channel-id` |
| `orche send` | 向已有会话发送 fire-and-forget 消息。 | `--session`, `<message>` |
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
orche send --help
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
- `codex_home`
- `codex_home_managed`
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

## 自动管理的 CODEX_HOME

每个 `orche` session 都会自动拥有自己的 Codex home。

例如，对于名为 `repo-codex-main` 的 session，`orche` 会使用类似下面的路径：

```text
/tmp/orche-codex-repo-codex-main/
```

执行 `session-new` 时，`orche` 会：

1. 如果目录不存在，则创建该临时目录
2. 将 `~/.codex/` 的内容复制进去
3. 写入一份 session 专属的 `hooks/discord-turn-notify.sh`
4. 重写复制后的 `config.toml`，让 notify 命令指向当前 session 和频道
5. 使用该目录作为 `CODEX_HOME` 启动 Codex

执行 `close` 时，`orche` 会删除自动管理的临时目录。

这样你可以得到：

- 每个会话一个 `CODEX_HOME`
- 每个会话一份复制后的 `config.toml`
- 每个会话独立的 notify 配置
- 每个会话独立的 Codex 历史记录和状态

如果你需要完全手动控制，仍然可以使用 `--codex-home` 作为高级覆盖项。

## 通知工作流

`tmux-orche` 使用现有的 Codex 原生 notify 管线，但在默认工作流中，它会自动管理一份按 session 复制出来的 hook。仓库中也提供了源码版本 [`scripts/notify-discord.sh`](./scripts/notify-discord.sh)，`orche` 会将它写入每个受管 `CODEX_HOME`。

这个 shell hook 被刻意做得很薄。它只负责把 Codex 的 notify 事件转发给 Python 通知管线 `orche _notify-discord`，因此 payload 解析、消息组装、registry 路由和 provider 投递都可以独立测试。

### `orche` 提供的能力

- `orche session-new` 会把活动会话上下文写入 `~/.config/orche/config.json`
- `orche session-new` 会创建每个 session 专属的临时 `CODEX_HOME`，并重写其中的 `config.toml`
- `orche turn-summary --session <name>` 以 CLI 形式暴露当前的 turn 摘要逻辑
- `orche _turn-summary --session <name>` 也保留为隐藏兼容别名
- `orche config get/set/list` 为通知密钥和频道设置提供稳定接口

这样职责划分会更清晰：

- Codex 负责发出 notify 事件
- hook 负责对外投递
- `orche` 负责提供会话元数据和摘要提取能力

### 自动 Notify 配置

默认工作流是自动的。

当你执行：

```bash
orche session-new \
  --cwd /path/to/repo \
  --agent codex \
  --name repo-codex-main \
  --discord-channel-id 123456789012345678
```

`orche` 会：

1. 将 `~/.codex/` 复制到 `/tmp/orche-codex-repo-codex-main/`
2. 将 `hooks/discord-turn-notify.sh` 写入这份复制后的 home
3. 重写复制后的 `config.toml`，让 notify 命令携带当前 session 和频道
4. 以 `CODEX_HOME=/tmp/orche-codex-repo-codex-main/` 启动 Codex

这意味着全局 `~/.codex/config.toml` 只作为基础模板使用，而每个运行中的 session 都会自动得到自己隔离的 notify 配置。

### 你仍然需要配置的内容

你仍然需要：

- 一份可复制的基础 `~/.codex/` 目录
- 通过以下任一方式提供 Discord 凭据：
  - `orche config set discord.bot-token ...`
  - 或 `DISCORD_BOT_TOKEN`
- 通过以下任一方式提供 Discord 频道 ID：
  - `orche config set discord.channel-id ...`
  - 或 `orche session-new --discord-channel-id ...`

示例：

```bash
orche config set discord.bot-token "your-token"
orche session-new \
  --cwd /path/to/repo \
  --agent codex \
  --name repo-codex-main \
  --discord-channel-id 123456789012345678
```

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
orche send --session repo-codex-main "review the latest changes"
```

3. 当 Codex 发出原生 notify 事件时，复制出来的 session 专属 hook 会读取：

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

如果你维护的是自定义 hook，且它仍然调用旧的 `orch.py _turn-summary`，请将该调用改为：

```bash
orche turn-summary --session "$session"
```

如果你希望尽量减少行为变化，也可以使用：

```bash
orche _turn-summary --session "$session"
```

通知链路的其余设计可以保持不变。

## 测试

notify 栈被拆成了几个可独立测试的小层：

- payload 解析与消息构造位于 `orche.notify.payload`
- provider 投递位于 `orche.notify.discord`
- registry 与多通知器 fan-out 位于 `orche.notify.registry` 和 `orche.notify.service`
- shell hook 只负责启动 `orche _notify-discord`

所有自动化测试都会 mock HTTP 投递，不会真的向 Discord 发送消息。

本地运行测试：

```bash
python -m pip install -e .[test]
pytest
```

CI 中会对 `orche.notify` 包强制要求 100% 覆盖率。

## License

MIT
