[English](README.md) · [Install Guide](https://github.com/parkgogogo/tmux-orche/raw/main/install.md)

# tmux-orche

面向 OpenClaw fire-and-forget 工作流的 tmux 后端 Codex 编排工具。

`tmux-orche` 让 OpenClaw 可以把任务交给 Codex 后立刻返回，之后再通过同一个持久化 tmux 会话继续接管。这样 OpenClaw 不需要在 Codex 后台工作期间持续消耗 token。

## OpenClaw 工作流

1. OpenClaw 使用 `orche session-new` 创建或复用一个 Codex 会话。
2. OpenClaw 使用 `orche send` 发送任务。
3. `orche` 立即返回。
4. Codex 在 tmux 中继续运行。
5. notify 到来后，OpenClaw 或其他 agent 再通过 `status`、`read` 或 `history` 检查同一个会话。
6. 会话会一直保留，直到显式关闭。

## 快速开始

创建或复用会话：

```bash
orche session-new \
  --cwd /path/to/repo \
  --agent codex \
  --name repo-codex-main \
  --discord-channel-id 123456789012345678
```

发送任务并立即返回：

```bash
orche send --session repo-codex-main "analyze the failing tests and propose a fix"
```

稍后再检查同一个会话：

```bash
orche status --session repo-codex-main
orche read --session repo-codex-main --lines 120
orche history --session repo-codex-main --limit 20
```

完成后关闭：

```bash
orche close --session repo-codex-main
```

## 安装

完整分步安装说明：<https://github.com/parkgogogo/tmux-orche/raw/main/install.md>

从 PyPI 安装：

```bash
pip install tmux-orche
```

使用 `uv` 安装：

```bash
uv tool install tmux-orche
```

从源码安装：

```bash
git clone https://github.com/parkgogogo/orche
cd orche
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install .
```

## 命令

- `orche session-new --cwd /repo --agent codex --name repo-codex-main --discord-channel-id 123456789012345678`
  创建或复用一个持久化的 Codex tmux 会话。
- `orche send --session repo-codex-main "review the recent auth changes"`
  向已有会话发送任务，并立即返回。
- `orche status --session repo-codex-main`
  检查该会话和 Codex 进程是否仍在运行。
- `orche read --session repo-codex-main --lines 80`
  读取该 live session 的最近终端输出。
- `orche history --session repo-codex-main --limit 20`
  查看该会话最近的本地控制动作。
- `orche close --session repo-codex-main`
  在任务完成后关闭会话。
- `orche config list`
  查看当前运行时配置。

## 配置

管理运行时设置：

```bash
orche config list
orche config get discord.channel-id
orche config set discord.channel-id 123456789012345678
orche config set discord.bot-token "$BOT_TOKEN"
orche config set discord.mention-user-id 123456789012345678
orche config set notify.enabled true
```

配置文件：

```text
~/.config/orche/config.json
```

状态目录：

```text
~/.local/share/orche/
```

## Troubleshooting

### Cancel a stuck turn

如果 Codex 卡住、任务方向错误，或者需要在不丢失 session 的前提下停止当前执行：

```bash
orche cancel --session repo-codex-main
```

这个命令会中断当前 Codex 回合，但保留 session，因此你仍然可以继续读取输出并发送修正后的任务。

与 close 的区别：

- `cancel`：中断当前回合，保留 session（适合卡住或仍在运行的任务）
- `close`：结束整个 session（适合任务已完成或已放弃）

## 前置条件

- `tmux`
- `tmux-bridge`
- `codex` CLI
- Python `3.9+`

## License

[MIT](LICENSE)
