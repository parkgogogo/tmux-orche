[English](README.md) · [Install Guide](https://github.com/parkgogogo/tmux-orche/raw/main/install.md)

# tmux-orche

一个面向 tmux agent orchestration 的 control plane。

`tmux-orche` 的核心目的只有一个：让 agent 可以稳定地调用其他 agent 作为 durable subagent，并且带有显式路由、可恢复的终端现场，以及必要时的人类接管能力。

它不是“给 tmux pane 再包一层命令”。它提供的是 agent 图上的稳定 session 名字、闭环路由和 live terminal 的检查 / 接管能力。

## 安装

完整安装说明：<https://github.com/parkgogogo/tmux-orche/raw/main/install.md>

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

## 它为什么存在

如果一个 agent 要去监督另一个 agent，你需要的就不只是“在某个 pane 里跑个命令”。

你真正需要的是：

- 每个 worker 都有稳定的 session 名字
- 结果能沿着显式路由返回
- 终端状态不会随着一次 prompt 结束而消失
- 不抢占 TTY 也能检查进度
- 自动化不够时还能直接接管 live terminal

`orche` 解决的就是这个空档。

## 两种闭环

`orche` 最有价值的地方，不是把命令发出去，而是把控制闭环收回来。

### OpenClaw -> Codex / Claude -> Discord

当 supervisor 是 OpenClaw，而且闭环需要回到 Discord / OpenClaw 时，用 `discord:<channel-id>`。

这条链路的样子是：

- OpenClaw 打开或复用一个 worker session
- worker 在 tmux 里持续运行并保留现场
- 完成或 needs-input 事件通过 Discord notify 回传
- OpenClaw 决定下一步继续调度什么

### Codex reviewer -> worker -> tmux bridge

当 supervisor 本身也是另一个 agent session 时，用 `tmux:<session>`。

这条链路的样子是：

- reviewer session 把任务委派给 worker session
- worker 通过 tmux bridge 把结果回送给 reviewer
- reviewer 再决定继续分派、继续 review，还是升级给人类接管

这才是 `orche` 的核心：让一个 agent session 可以可靠地寻址和驱动另一个 agent session。

## 为什么必须是命名 session

原始 tmux pane 不是 control plane。

有了 `orche`，你操作的是 `repo-reviewer`、`repo-worker`、`auth-fixer`，不是 `%17`。

这是关键区别，因为一个命名 session 可以稳定携带：

- 工作目录
- agent 类型
- 持久化 tmux pane
- 显式 notify 路由
- 后续检查和人工接管能力

## 核心工作流

标准流程就是：

1. `open`
2. `prompt`
3. 离开
4. 之后再 `status` 或 `read`
5. 需要时 `attach`

## 快速开始

### 原生打开并立即接管的快捷命令

在当前目录打开一个新的 native session 并立刻 attach：

```bash
orche codex --model gpt-5.4
orche claude -- --print --help
```

这些快捷命令会：

- 始终把当前目录作为 `cwd`
- 把后续参数透传给底层 agent CLI
- 生成一个新的 session 名，例如 `<repo>-<agent>-<random>`

### 通过 tmux bridge 构建 reviewer-worker 闭环

先打开 reviewer：

```bash
orche open --cwd /repo --agent codex --name repo-reviewer
```

再打开一个把结果回给 reviewer 的 worker：

```bash
orche open \
  --cwd /repo \
  --agent codex \
  --name repo-worker \
  --notify tmux:repo-reviewer
```

发送任务：

```bash
orche prompt repo-worker "implement the parser refactor"
```

稍后检查 reviewer：

```bash
orche read repo-reviewer --lines 120
orche status repo-worker
```

需要时直接接管 worker：

```bash
orche attach repo-worker
```

### 通过 Discord 构建 OpenClaw 闭环

打开一个把结果回给 Discord 的 worker：

```bash
orche open \
  --cwd /repo \
  --agent codex \
  --name repo-worker \
  --notify discord:123456789012345678
```

发送任务：

```bash
orche prompt repo-worker "analyze the failing tests and propose a fix"
```

之后检查或直接接管：

```bash
orche status repo-worker
orche read repo-worker --lines 120
orche attach repo-worker
```

## 适合什么场景

`tmux-orche` 特别适合这些情况：

- 一个 reviewer session 协调多个 worker
- OpenClaw 通过 Discord notify 监督 Codex 或 Claude
- worker session 需要接受多轮 follow-up
- 需要在 tmux 内做显式 session-to-session 路由
- 自动化卡住时还能随时接管 live terminal

如果你只是想执行一次短命令、执行完就结束，那它就不一定有优势。

## Managed 和 Native 的区别

### Managed session

普通 orchestration 用 managed 模式：

```bash
orche open --cwd /repo --agent codex --name repo-worker --notify tmux:repo-reviewer
```

这是默认推荐方式，因为 `orche` 才能完整管理 session 元数据和 routing。

### Native session

如果你需要透传原生 agent CLI 参数，用 native 模式：

```bash
orche open --cwd /repo --agent claude -- --print --help
```

规则：

- 原生 agent 参数必须放在 `--` 后面
- native session 不使用 `--notify`
- 不要把 raw agent args 和 managed notify routing 混用

## 命令模型

- `orche open`
  创建或复用一个可寻址的 control endpoint。
- `orche codex` / `orche claude`
  为当前目录打开一个新的 native session，并立即 attach。
- `orche prompt`
  往现有 session 委派工作。
- `orche status`
  看 pane 和 agent 是否活着，以及是否还有 pending turn。
- `orche read`
  不接管 TTY 的前提下读取最近终端输出。
- `orche attach`
  把当前终端接到 live tmux session。
- `orche input`
  输入文本但不按 Enter。
- `orche key`
  发送特殊按键，比如 `Enter`、`Escape`、`C-c`。
- `orche list`
  列出本地已知 session。
- `orche cancel`
  中断当前 turn，但保留 session。
- `orche close`
  结束 session 并清理状态。
- `orche whoami`
  输出当前 session id。
- `orche config`
  读取或修改共享运行时配置。

## CLI 入口快捷方式

这些短参数只用于 CLI 入口层：

```bash
orche -h
orche -v
orche config -h
```

说明：

- `-h` 支持根命令和命令组
- `-v` 只支持根命令
- leaf 命令仍然使用 `--help`，例如 `orche attach --help`

## Notify 和路由

Notify 的作用是把控制闭环收回来。

`orche open --notify` 接受：

- `tmux:<target-session>`
- `discord:<channel-id>`

当接收方是另一个 agent session 时，用 `tmux:<session>`：

```bash
orche open --cwd /repo --agent codex --name repo-worker --notify tmux:repo-reviewer
```

当接收方是 OpenClaw / Discord 侧的 supervisor 时，用 `discord:<channel-id>`：

```bash
orche open --cwd /repo --agent codex --name repo-worker --notify discord:123456789012345678
```

说明：

- 路由必须显式声明，没有隐式全局默认值
- 如果要换 notify 目标，直接开一个新 session
- `tmux` 路由用于 agent-to-agent 闭环
- `discord` 路由用于 OpenClaw 或外部 supervision 闭环

## 配置

```bash
orche config list
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

## 前置条件

- `tmux`
- `codex` CLI 和/或 `claude` CLI
- Python `3.9+`

## License

[MIT](LICENSE)
