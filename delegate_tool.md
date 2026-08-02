# delegate_tool.py 学习笔记

## 整体架构：父子 Agent 模型

hermes-agent 的委托系统基于 **一父多子** 的架构。父 agent 通过 `delegate_task` 工具派发子 agent，每个子 agent 是独立的 `AIAgent` 实例，运行自己完整的 agent loop。

```
父 AIAgent                          子 AIAgent（独立实例）
├─ agent loop (run_conversation)    ├─ agent loop (run_conversation)
├─ 调用 delegate_task 工具           ├─ 自己的 messages 历史
├─ 被 heartbeat 线程 touch 保持活跃  ├─ 自己的 tool 调用
└─ 等待子 agent 完成                 └─ 独立的 LLM API 调用
```

## 核心方法调用链

### 1. `delegate_task()` — 入口

**位置：** [delegate_tool.py:2369](tools/delegate_tool.py#L2369)

作为 **tool** 暴露给 LLM，模型可以这样调用：

```python
delegate_task(
    goal="Debug why tests fail",
    context="Error: assertion in test_foo.py line 42"
)
```

**两种模式：**

| 模式 | 调用方式 | 对应逻辑 |
|---|---|---|
| Single | `delegate_task(goal="...", context="...")` | 包装成单元素 `task_list` |
| Batch | `delegate_task(tasks=[{goal:"..."}, {goal:"..."}])` | 直接使用 `tasks` 数组 |

**参数说明：**

| 参数 | 作用 |
|---|---|
| `goal` | 子 agent 的任务目标（Single 模式） |
| `context` | 补充上下文，如错误日志、文件路径等 |
| `tasks` | 批量任务数组（Batch 模式），每项含 `goal`/`context`/`role` |
| `role` | `"leaf"`（默认，不能再委托）或 `"orchestrator"`（可继续委托） |
| `background` | `true` 则走 `async_delegation.py` 的异步路径，父 agent 不等待 |
| `max_iterations` | **模型传的值会被忽略**，实际用 config.yaml 中的 `delegation.max_iterations` |

**关键步骤：**

```
delegate_task()
  ├─ 深度限制检查（默认 max_spawn_depth=2）
  ├─ config 加载（max_iterations 取自配置，忽略模型传值）
  ├─ 凭证解析（可路由到不同 provider:model）
  ├─ 任务规范化（goal → task_list 或直接使用 tasks 数组）
  ├─ _build_child_agent() × N    → 构建所有 AIAgent 实例
  ├─ _execute_and_aggregate()    → 运行所有子 agent
  │    ├─ 单任务：直接调用 _run_single_child()
  │    └─ 多任务：DaemonThreadPoolExecutor 并行提交 _run_single_child
  └─ 返回结果 JSON
```

### 2. `_build_child_agent()` — 构建子 agent

**位置：** [delegate_tool.py:1044](tools/delegate_tool.py#L1044)

**职责：** 创建 `AIAgent` 实例，配置模型/凭证/角色/深度。**不运行**，只构建。

```python
from run_agent import AIAgent

child = AIAgent(...)  # 每个子 agent 是全新的独立实例
```

关键配置：
- **角色降级：** `orchestrator` → `leaf` 如果超出深度限制或 kill switch 开启
- **凭证覆盖：** 可路由到不同 provider:model（如便宜模型跑子任务）
- **`_subagent_id`：** 构造时生成，用于 TUI 注册、文件状态协调、事件关联

### 3. `_run_single_child()` — 运行子 agent

**位置：** [delegate_tool.py:1746](tools/delegate_tool.py#L1746)

**职责：** 完整的子 agent 生命周期管理。调用 `child.run_conversation()` 真正发起 LLM 对话循环。

**不调用 `_build_child_agent`** —— `child` 已由上游构建好直接传入。

```python
def _run_single_child(
    task_index: int,
    goal: str,
    child=None,        # ← 已构建好的 AIAgent 实例
    parent_agent=None,
    **_kwargs,
) -> Dict[str, Any]:
```

**完整执行流程：**

```
1. 凭证租约 — 从子 agent 凭证池租用凭证，绑定到 child
2. 启动心跳线程 — 保活父 agent，防止 gateway 超时
3. TUI 注册 — 注册到 _active_subagents，支持 kill/pause/查状态
4. 文件状态快照 — 记录父 agent 已知的文件读取列表
5. 提交到 DaemonThreadPoolExecutor:
     _run_with_thread_capture()
       └─ child.run_conversation(          ← 真正的 LLM agent loop
              user_message=goal,
              task_id=child_task_id,
              stream_callback=_relay_child_text,  ← 流式输出转发
          )
     _child_future.result(timeout=...)       ← 阻塞等待 / 超时处理
6. 超时处理 — 中断子 agent，转储诊断（0-API-call 卡死时）
7. 结果提取 — summary, api_calls, tokens, model, cost, tool_trace
8. 进度回调 — child_progress_cb("subagent.complete", ...)
9. finally: 停止心跳、注销 TUI、释放凭证、恢复 tool names
```

**返回值：**

| 字段 | 含义 |
|---|---|
| `status` | `"completed"` / `"interrupted"` / `"failed"` / `"error"` / `"timeout"` |
| `summary` | 子 agent 的最终回复文本 |
| `api_calls` | LLM API 调用次数 |
| `duration_seconds` | 运行耗时 |
| `model` | 使用的模型 |
| `tokens` | `{"input": N, "output": N}` |
| `tool_trace` | 工具调用记录列表 |
| `exit_reason` | `"completed"` / `"interrupted"` / `"max_iterations"` / `"timeout"` / `"error"` |
| `error` | 错误信息（失败时） |
| `stale_paths` | 兄弟节点修改的文件路径（文件状态协调） |
| `diagnostic_path` | 0-API-call 超时的诊断转储路径 |

### 4. `_run_with_thread_capture()` — 真正调用 run_conversation

**位置：** [delegate_tool.py:1946](tools/delegate_tool.py#L1946)

```python
def _run_with_thread_capture():
    _worker_thread_holder["t"] = threading.current_thread()
    return child.run_conversation(
        user_message=goal,
        task_id=child_task_id,
        stream_callback=_relay_child_text,
    )

_child_future = _timeout_executor.submit(_run_with_thread_capture)
result = _child_future.result(timeout=child_timeout)
```

包在闭包里提交到 `DaemonThreadPoolExecutor` 的原因：
1. **超时控制** — `future.result(timeout=...)` 限时等待
2. **堆栈诊断** — `_worker_thread_holder` 捕获线程引用，超时时 dump Python 堆栈（`#14726`）
3. **流式转发** — `stream_callback=_relay_child_text` 实时转发子 agent 输出到父 agent 进度通道

## 心跳线程 (`_heartbeat_thread`)

**位置：** [delegate_tool.py:1794-1863](tools/delegate_tool.py#L1794)

**核心矛盾：** 父 agent 在 `delegate_task` 中被同步阻塞，自己不产生活动，gateway 会误判父 agent 已死并断开。

**解决方案：** daemon 线程周期性调用 `parent_agent._touch_activity()`，让 gateway 看到父 agent 仍在活跃。

**额外防护 —— 停滞检测：**

追踪子 agent 的 `(api_call_count, current_tool)` 是否推进：
- 连续 N 周期无进展 → 认为子 agent 卡死 → 停止 touch → gateway 正常超时

| 场景 | 容忍度 |
|---|---|
| 子 agent 执行工具中（如长命令） | `_HEARTBEAT_STALE_CYCLES_IN_TOOL`（较长） |
| 子 agent 空闲（等待 LLM 响应） | `_HEARTBEAT_STALE_CYCLES_IDLE`（较短） |

本质：**带死锁检测的保活机制。**

## 异步委托（`background=true`）

**位置：** [async_delegation.py](tools/async_delegation.py)

当 `background=true` 时，不走 `_run_single_child` 的同步等待路径，而是通过 `async_delegation.py` 将子 agent 提交到共享 daemon 线程池：

```
dispatch_async_delegation()
  → 容量检查 → 写入 memory records + SQLite
  → executor.submit(worker)
    → runner() 执行子 agent
    → _finalize()
      → _push_completion_event()
        → SQLite 持久化完成事件
        → process_registry.completion_queue.put(evt)
          → CLI/gateway 轮询队列，agent 空闲时作为新 turn 注入对话
```

这保证了：
- **消息角色交替合法** — 完成事件作为新 turn 注入，不会插入到 tool-result 和 assistant-message 之间
- **会话隔离** — 事件携带 `session_key`，正确路由回发起会话
- **跨进程恢复** — SQLite 持久化 + 投递保证，进程重启后可恢复未投递的完成事件

## Session 生命周期

| 操作 | 效果 | 数据是否保留 |
|---|---|---|
| **Session End** (`end_session`) | 设置 `ended_at` 和 `end_reason` | ✅ 行和消息都保留 |
| **Session Delete** (`delete_session`) | 物理删除行和消息 | ❌ 永久删除 |
| **/new 命令** | End 旧 session → 创建新 session（新 session_id） | ✅ 旧 session 保留 |

`/new` 时 `interrupt_for_session()` 通过 `session_key` + `parent_session_id` 双重匹配终止旧 session 的后台子 agent，防止孤儿任务泄漏。

## Model 参数安全

`delegate_task` 签名中的 `max_iterations` 参数**模型传的值会被忽略**，实际使用 `config.yaml` 中的 `delegation.max_iterations`。设计原因：防止模型通过 tool schema 注入极小的值导致子 agent 跑不完就被截断。

## 关键文件索引

| 文件 | 职责 |
|---|---|
| [tools/delegate_tool.py](tools/delegate_tool.py) | delegate_task 工具定义、子 agent 构建+运行 |
| [tools/async_delegation.py](tools/async_delegation.py) | 异步委托生命周期管理（派发/完成/中断/持久化/投递保证） |
| [run_agent.py](run_agent.py) | `AIAgent` 类 + `run_conversation()` agent loop |
| [hermes_state.py](hermes_state.py) | Session 持久化（SQLite），end/delete/reopen 操作 |
| [gateway/slash_commands.py](gateway/slash_commands.py) | `/new` 命令处理，触发 session reset + 中断子 agent |
