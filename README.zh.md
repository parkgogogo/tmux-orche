<h1 align="center">tmux-orche 🎼</h1>

<p align="center">
  <a href="https://github.com/parkgogogo/tmux-orche/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License"></a>
  <img src="https://img.shields.io/badge/Python-3.9%2B-blue" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/tmux-required-green" alt="tmux required">
</p>

<p align="center">
  <b>基于 tmux 的 agent 编排控制面板。</b><br>
  开 agent、派任务、收结果、随时接管。
</p>

<p align="center">
  <a href="README.md">English</a> · <a href="#快速开始">快速开始</a> · <a href="#安装">安装</a> · <a href="./docs">文档</a>
</p>

<p align="center">
  <img src="./assets/Ghostty.gif" alt="Codex 和 Claude 通过 orche 互相打招呼" width="720">
</p>

## tmux-orche 是什么？

一个 agent 把任务交给另一个 agent 之后，最难的不是"怎么发出去"，而是**怎么把结果收回来**。

没有 `orche` 的时候，你只能不停轮询 worker 的状态，每轮都在烧 token。长任务尤其折磨——worker 可能跑十分钟，你只能干等或者写一堆脆弱的重试逻辑。如果 worker 卡住了，你往往要浪费几十轮轮询才能发现。等你终于想跳进终端手动排查，才发现根本不知道 agent 跑在哪个 pane 里。

**tmux-orche** 把这些问题一次解决：它把 tmux session 变成可命名、可持久的 agent worker。打开 session、丢任务、走人。worker 在 tmux 里保持运行，终端状态完整保留。跑完了，结果自动送回来——不用轮询。如果哪里卡住了，你或者另一个 agent 可以随时 attach 进去接手。

- **稳定的 session 名称**，不用再对着 `%17` 这种 pane ID 猜
- **结果自动路由**，送到该去的地方
- **终端状态不丢**，不会因为一轮 prompt 结束就没了
- **随时可以接管**，自动化搞不定的时候人直接跳进去

## 工作原理

```
                        orche open --notify tmux:supervisor
                        orche prompt worker "执行任务"
                                    │
  ┌─────────────┐                   ▼                  ┌─────────────┐
  │  Supervisor  │               tmux session          │   Worker    │
  │  (Codex /    │◂── notify ──  终端状态持久保留  ──▸   │  (Codex /   │
  │   Claude)    │                                     │   Claude)   │
  └─────────────┘                                      └─────────────┘
        │                                                     │
        │  收到结果，                                          │  跑完了，
        │  下一轮自动开始                                      │  触发 notify
        ▼                                                     ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │                        Notify 通道                               │
  │   tmux:session   ·   discord:channel   ·   telegram:chat        │
  └──────────────────────────────────────────────────────────────────┘
```

1. **Supervisor** 调 `orche open`，创建一个有名字、有 notify 路由的 worker session
2. **Worker** 在 tmux 里跑，终端状态完整保留，随时可以查看
3. 任务跑完，**notify** 把结果送回来（tmux / Discord / Telegram 都行）
4. Supervisor 自动进入下一轮——**不轮询，不重试**

## 快速开始

```bash
# 1. 安装
curl -fsSL https://github.com/parkgogogo/tmux-orche/raw/main/install.sh | sh

# 2. 获取当前 tmux session（在 tmux 里执行）
current_session="$(orche whoami)"

# 3. 开 worker，派任务，走人
orche open \
  --cwd ./my-repo \
  --agent codex \
  --name my-worker \
  --notify "tmux:${current_session}" \
  --prompt "重构 auth 模块"

# 4. 跑完了结果会自动送回你的 session。
#    中途想看看进度：
orche status my-worker        # 还活着吗？在跑吗？
orche read my-worker          # 看看终端输出
orche attach my-worker        # 直接接管终端
```

## 使用场景

### 1. Codex / Claude 多 Agent 协作

让多个 agent 在 tmux 里配合干活。比如 Claude 做 review、Codex 写代码：

```bash
# 开一个 reviewer session
orche open --cwd ./repo --agent claude --name repo-reviewer

# 开一个 worker，跑完后结果自动送给 reviewer
orche open \
  --cwd ./repo \
  --agent codex \
  --name repo-worker \
  --notify tmux:repo-reviewer

# 派任务，走人
orche prompt repo-worker "重构 auth 模块"
```

![Claude + Codex 协作](./assets/b9f91b78-1852-453a-8b0d-2cae29185174.png)

*上图：一个 Codex supervisor 同时调 2 个 Codex 和 2 个 Claude 并行做 code review。*

> **小技巧：** 想快速开一个默认 agent session 可以用简写：
> ```bash
> orche codex   # 等价于：orche open --agent codex
> orche claude  # 等价于：orche open --agent claude
> ```

### 2. OpenClaw 监督闭环

`orche` 可以让 OpenClaw 把耗时长、计算重的任务丢给 Codex 或 Claude 去跑。Worker 在 tmux 里保持运行，跑完后通过 Discord 通知 OpenClaw，形成闭环。

> 需要给 Codex 单独建一个 Discord Bot，通过 `orche config` 配好。详见[配置](#配置)。

```bash
orche open \
  --cwd ./repo \
  --agent codex \
  --name repo-worker \
  --notify discord:123456789012345678

orche prompt repo-worker "分析一下失败的测试用例"
```

![OpenClaw 监督](./assets/openclaw-supervision-loop.png)

## 命令

| 命令 | 说明 |
|------|------|
| `orche open` | 创建或复用一个带 notify 路由的 worker session |
| `orche prompt` | 给已有 session 派任务 |
| `orche status` | 看 agent 状态和当前 turn |
| `orche read` | 看终端输出，不抢 TTY |
| `orche attach` | 接管终端 |
| `orche close` | 关掉 session，清理状态 |
| `orche input` | 往 session 里打字，不按回车 |
| `orche key` | 发特殊按键（`Enter`、`Escape`、`C-c`） |
| `orche list` | 列出本地已有的 session |
| `orche cancel` | 中断当前 turn，session 保留 |
| `orche config` | 查看或改运行时配置 |

完整参考：[docs/commands.md](./docs/commands.md)

## 安装

### 依赖

- [tmux](https://github.com/tmux/tmux)
- Python 3.9+，仅在用 `pip`、`uv` 或源码安装时需要
- `codex` CLI 和/或 `claude` CLI（看你用哪个 agent）

### 快速安装

装预编译二进制，不需要本机 Python：

```bash
curl -fsSL https://github.com/parkgogogo/tmux-orche/raw/main/install.sh | sh
```

更新：

```bash
orche update
```

或者用 `uv`：

```bash
uv tool install tmux-orche
```

`install.sh` 和 `uv` 都不合适的话，看 `install.md`，有 `pip`、源码安装和常见问题。

装完验证一下：

```bash
orche --help
```

### 给 Agent

想让另一个 agent 替你装，把这个链接丢给它：

`https://raw.githubusercontent.com/parkgogogo/tmux-orche/main/install.md`

### 装 SKILL（推荐）

装了 SKILL，你的 agent 就知道怎么用 `orche`。选你用的 agent：

<details>
<summary>Codex / Claude / OpenClaw SKILL 安装命令</summary>

**Codex：**
```bash
mkdir -p ~/.codex/skills/orche
curl -fsSL https://raw.githubusercontent.com/parkgogogo/tmux-orche/main/skills/codex-claude/SKILL.md \
  -o ~/.codex/skills/orche/SKILL.md
```

**Claude：**
```bash
mkdir -p ~/.claude/skills/orche
curl -fsSL https://raw.githubusercontent.com/parkgogogo/tmux-orche/main/skills/codex-claude/SKILL.md \
  -o ~/.claude/skills/orche/SKILL.md
```

**OpenClaw：**
```bash
mkdir -p ~/.openclaw/skills/orche
curl -fsSL https://raw.githubusercontent.com/parkgogogo/tmux-orche/main/skills/openclaw/SKILL.md \
  -o ~/.openclaw/skills/orche/SKILL.md
```

</details>

## 配置

配置文件在 `~/.config/orche/config.json`（设了 `XDG_CONFIG_HOME` 就在 `$XDG_CONFIG_HOME/orche/config.json`）。

```bash
# Discord 通知
orche config set discord.bot-token "$BOT_TOKEN"
orche config set discord.webhook-url "$WEBHOOK_URL"

# managed session 空闲超时（默认 43200 秒，<=0 不过期）
orche config set managed.ttl-seconds 1800

# 自定义 Claude CLI 路径
orche config set claude.command /opt/tools/claude-wrapper
```

```bash
orche config list              # 看全部
orche config get <key>         # 看某项
orche config reset <key>       # 恢复默认
```

完整参考：[docs/config.md](./docs/config.md)

## 扩展

`agent` 和 `notify` 都是插件，可以自己写：

- [写 Agent 插件](./docs/agent-plugin-dev.md)
- [写 Notify 插件](./docs/notify-plugin-dev.md)

### 路线图

- [x] Discord 通知
- [x] Telegram 通知
- [ ] 更多 agent（如 pi）
- [ ] Codex 作为独立 subagent，有自己的 skills / MCP

## 致谢

`tmux-orche` 的设计受 [ShawnPana/smux](https://github.com/ShawnPana/smux) 启发，在 tmux session 管理和 agent 编排上给了我们很多思路。

## License

[MIT](LICENSE)
