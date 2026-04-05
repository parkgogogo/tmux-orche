# 真实端到端测试

这里的端到端测试只指真实链路：

- 真实 `orche` CLI
- 真实 `tmux`
- 真实 `codex` CLI
- 真实 session 之间的 prompt / notify / read 流转

不把 fake tmux、fake runtime、`CliRunner + monkeypatch` 这类测试算作端到端测试。

## 当前 pytest E2E 场景

### `tests/test_notify_e2e.py`

覆盖真实 notify 路由行为：

- `tmux-bridge` 单目标通知
- 同一目标的并发通知串行化
- 显式路由 / session 绑定 / 全局配置优先级
- session 绑定覆盖全局 provider，且不发生 fanout

### `tests/test_session_collaboration_e2e.py`

覆盖真实 reviewer / worker 协作链路：

- 起一个真实 reviewer session，并覆盖 `codex` 和 `claude`
- 起一个真实 worker session，并把 notify 回 reviewer
- worker 覆盖 `codex` 和 `claude`
- 组合矩阵覆盖 `codex->codex`、`codex->claude`、`claude->codex`、`claude->claude`
- 给 worker 发送固定 prompt
- 验证 reviewer 收到来自 worker 的真实 `tmux-bridge` notify
- 验证 worker 输出了预期固定 token

## 运行方式

先确保环境满足：

- 已安装 `tmux`
- 已安装 `codex`
- 已安装 `claude`
- `codex` 已登录，可正常启动
- `claude` 已登录，可正常启动

运行：

```bash
ORCHE_RUN_E2E=1 python3 -m pytest -q tests/test_notify_e2e.py tests/test_session_collaboration_e2e.py
```

可选超时：

```bash
ORCHE_RUN_E2E=1 ORCHE_E2E_TIMEOUT=240 python3 -m pytest -q tests/test_notify_e2e.py tests/test_session_collaboration_e2e.py
```

## 约束

- 默认不跑真实 E2E，必须显式设置 `ORCHE_RUN_E2E=1`
- 真实 E2E 只断言稳定协议面，不断言模型自由文本风格
- 如果 `codex` 未登录或本机缺少依赖，测试应直接跳过，而不是伪造通过
