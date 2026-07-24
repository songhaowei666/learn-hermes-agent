# interruptible_api_call — 可中断 API 调用机制

## 一句话总结

API 请求被封装在后台线程中执行，主线程以 300ms 间隔轮询等待，在此期间同时监听中断事件、陈旧调用超时和 Codex 流看门狗。发生中断时，强制关闭连接并抛出 `InterruptedError`，部分响应**不会**被注入对话历史。

## 完整调用链

```
run_conversation()                          # agent/conversation_loop.py:523
  └─ _perform_api_call()                    # 闭包, conversation_loop.py:1320
       └─ agent._interruptible_api_call()   # run_agent.py:4605 (转发器)
            └─ interruptible_api_call()     # agent/chat_completion_helpers.py:362 (核心实现)
```

## 架构图

```
┌─ 主线程（轮询循环）────────────────────┐  ┌─ 工作线程（守护线程）──────────────┐
│                                        │  │                                   │
│  t = Thread(target=_call); start()     │  │  client = _create_client()         │
│                                        │  │  response = client.create(...)      │
│  while t.is_alive():                   │  │  ◀── 阻塞在 HTTP recv() 上          │
│    t.join(0.3)        ──每 300ms──▶   │  │                                   │
│    检查 _interrupt_requested           │  │                                   │
│    检查 stale timeout                   │  │                                   │
│    检查 Codex 看门狗                    │  │                                   │
│                                        │  │                                   │
│  ── 中断到达 ──                        │  │                                   │
│  _request_cancelled = True             │  │                                   │
│  _close_request_client_once()  ────────▶│  │  socket.shutdown() → recv()       │
│                                        │  │  解除阻塞并抛出 OSError           │
│  raise InterruptedError                │  │  except: _request_cancelled       │
│                                        │  │    为 True → return（静默退出）   │
│                                        │  │  finally: close(client)           │
└────────────────────────────────────────┘  └───────────────────────────────────┘
```

## 逐层分解

### 1. Cron 快速路径（无工作线程）

```python
# agent/chat_completion_helpers.py:295-307
if should_use_direct_api_call(agent):
    return direct_api_call(agent, api_kwargs)
```

对于 `platform == "cron"` 的非交互式上下文，完全跳过工作线程以避免嵌套线程池死锁。中断仍然有效——但因为是同步调用后的检查，不是轮询，所以不是即时的。

- **触发条件**：`platform == "cron"` AND `api_mode == "chat_completions"` AND `provider != "moa"`
- **原因**：问题 #62151 —— 将 OpenRouter 的聊天补全路径嵌套在网关 cron 线程栈中会在 socket 打开前阻塞

### 2. 生成工作线程（`_call` 函数）

```python
# agent/chat_completion_helpers.py:443-470
def _call():
    try:
        result["response"] = _dispatch_nonstreaming_api_request(
            agent, api_kwargs,
            make_client=lambda reason: _set_request_client(
                agent._create_request_openai_client(...)
            ),
        )
    except Exception as e:
        if _request_cancelled["value"]:
            # 主线程强制关闭连接 → 静默吞掉传输错误
            return
        result["error"] = e           # 真正的网络错误 → 上报
    finally:
        _close_request_client_once("request_complete")
```

每 次 API 调用创建独立的 OpenAI client 实例（通过 `_create_request_openai_client`），避免中断关闭一个 client 后影响其他请求。

### 3. 陌生人线程保护（`#29507`）

```python
# agent/chat_completion_helpers.py:411-441
def _close_request_client_once(reason: str) -> None:
    # 判断调用者是否就是工作线程自己
    stranger_thread = (
        request_client is not None
        and owner_tid is not None
        and owner_tid != threading.get_ident()
    )
    if not stranger_thread:
        # 工作线程自己 → 正常完整关闭
        request_client_holder["client"] = None
        client.close()
    else:
        # 中断/陈旧检测线程 → 只 shutdown socket，不关闭 client
        agent._abort_request_openai_client(client, reason=reason)
```

**为什么需要区分**（`#29507`）：如果中断线程直接调用 `client.close()`，内核可能立即回收该 TLS socket 的 FD 并重新分配给 `kanban.db`（SQLite 文件）。但工作线程上的 SSL BIO 仍然存活——它会对这个 FD 执行最后一次 TLS `write()`，将 24 字节的 TLS 应用数据记录写入 SQLite 文件头，导致数据库损坏。

**解决方案**：陌生人线程只执行 `socket.shutdown(SHUT_RDWR)` —— 这解除了工作线程 `recv()` 的阻塞，让工作线程自己完成完整的 `client.close()`。

### 4. 主线程轮询循环

```python
# agent/chat_completion_helpers.py:576-766
while t.is_alive():
    t.join(timeout=0.3)   # 每 300ms 醒来一次

    # 检查 1: Codex TTFB 看门狗（连接已打开但从未发送任何 SSE 事件）
    # 检查 2: Codex 流空闲看门狗（事件流中途停止）
    # 检查 3: 陈旧调用超时（非流式调用完全没有响应）
    # 检查 4: agent._interrupt_requested ← 用户中断
```

轮询中同时执行四类检查，300ms 的粒度保证了中断响应的及时性（最坏情况 300ms 延迟）。

### 5. `_request_cancelled` 详解（问题 `#6600` —— 级联中断挂起）

#### 竞态条件场景

```
主线程                              │  工作线程
                                    │
  t.join(0.3) ← 开始                │  recv() 阻塞中
                                    │
  ⚡ 中断到达                        │
  _request_cancelled = True         │
  close(connection) ───────────────▶│  recv() 立即失败
                                    │  except: result["error"] = OSError
                                    │  finally: cleanup → 线程退出
  t.join(0.3) 返回                   │
  t.is_alive() → False！            │  （线程已经终止）
                                    │
  while 循环退出 ◀── 错过了中断检查！  │
                                    │
  if result["error"]:               │
      raise result["error"]  ←──  OSError 被错误地向上传播！
```

#### 问题本质

工作线程在 0.3 秒的 join 窗口内完成，主线程的 while 循环看到 `t.is_alive() == False` 后干净退出——**从未到达 `if agent._interrupt_requested:`** 检查。`result["error"]` 中的传输错误（`OSError`、`RemoteProtocolError` 等）被当作真正的网络故障向上传播。

**后果**：在 `run_conversation()` 中，这个错误进入重试/退避/回退恢复链。用户会看到一个"重试中……"旋转图标，但中断已经被处理，队列里没有新的工作——系统无限挂起。这就是**级联中断挂起**。

#### 修复方案

`_request_cancelled` 是一个主线程 → 工作线程的信号通道：

```python
# 主线程在 close 之前先设置
_request_cancelled["value"] = True

# 工作线程在 catch 中检查
except Exception as e:
    if _request_cancelled["value"]:
        return                # ← "是主线程关了我，不算错误"
    result["error"] = e       # ← 真正的网络错误
```

#### 修复后的时序

```
主线程                              │  工作线程
                                    │
  _request_cancelled = True  ──────▶│  （在 close 之前设置）
  close(connection) ───────────────▶│  recv() 抛出 OSError
                                    │  except: _request_cancelled？
                                    │  True → return（不设置 error）
  t.is_alive() → False              │
  while 循环退出                     │
  result["error"] is None → 跳过    │
  return result["response"] → None  │
```

工作线程静默退出，`result["error"]` 保持 `None`，不会有虚假错误进入重试逻辑。

### 6. InterruptedError 被捕获后的处理

```python
# agent/conversation_loop.py:2300-2323
except InterruptedError:
    interrupted = True

    # 保留已流式传输到屏幕的部分助手文本
    _partial = agent._strip_think_blocks(
        getattr(agent, "_current_streamed_assistant_text", "") or ""
    ).strip()
    if _partial:
        messages.append({"role": "assistant", "content": _partial})
        final_response = _partial
    else:
        final_response = "Operation interrupted: waiting for model response (...)"

    agent._persist_session(messages, conversation_history)
    break
```

关键设计：
- 已呈现给用户的流式文本**被保留并写入对话历史**，避免模型"忘记自己刚才说了什么"
- 思考块（`<think>...</think>`）通过 `_strip_think_blocks()` 被剥离，防止推理内容泄露
- 会话立即持久化

```python
# agent/conversation_loop.py:4229-4232
if interrupted:
    _turn_exit_reason = "interrupted_during_api_call"
    break  # 跳过所有响应处理
```

### 7. 中断如何被触发

```python
# run_agent.py:2665-2727
def interrupt(self, message):
    self._interrupt_requested = True
    self._interrupt_message = message

    # ① 中止内联 cron 请求
    _abort_active_request("interrupt_abort")

    # ② 给代理的执行线程设置工具级中断信号
    _set_interrupt(True, self._execution_thread_id)

    # ③ 传播到所有并发工具工作线程
    for worker_tid in self._tool_worker_threads:
        _set_interrupt(True, worker_tid)

    # ④ 递归传播到子代理
    for child in self._active_children:
        child.interrupt(message)
```

## 关键文件索引

| 文件 | 行号 | 内容 |
|------|------|------|
| `agent/chat_completion_helpers.py` | 295-307 | `should_use_direct_api_call` — cron 快速路径条件 |
| `agent/chat_completion_helpers.py` | 310-360 | `direct_api_call` — cron 内联调用实现 |
| `agent/chat_completion_helpers.py` | 362-773 | `interruptible_api_call` — 核心实现（工作线程 + 轮询 + 看门狗） |
| `agent/chat_completion_helpers.py` | 411-441 | `_close_request_client_once` — 陌生人线程保护 |
| `agent/chat_completion_helpers.py` | 443-470 | `_call` — 工作线程函数 |
| `agent/chat_completion_helpers.py` | 576-766 | 轮询循环体（四次检查） |
| `agent/chat_completion_helpers.py` | 746-766 | 中断检查 + `InterruptedError` 抛出 |
| `agent/conversation_loop.py` | 523 | `run_conversation` — 对话主循环入口 |
| `agent/conversation_loop.py` | 648-653 | 循环顶部中断检查 |
| `agent/conversation_loop.py` | 1320-1331 | `_perform_api_call` 闭包 |
| `agent/conversation_loop.py` | 1583-1609 | 退避等待期间中断检查 |
| `agent/conversation_loop.py` | 2300-2323 | `InterruptedError` 捕获 + 部分文本保留 |
| `agent/conversation_loop.py` | 4229-4232 | 中断后跳过响应处理 |
| `run_agent.py` | 2665-2727 | `interrupt()` — 中断触发与传播 |
| `run_agent.py` | 2729-2744 | `clear_interrupt()` — 中断清除 |
| `run_agent.py` | 4605-4608 | `_interruptible_api_call` — 转发器 |

## 设计保证总结

| 保证 | 实现方式 |
|------|----------|
| 部分响应不注入历史 | `InterruptedError` 在工作线程设置 `result["response"]` 之前抛出 |
| 已流式文本被保留但清理 | `_strip_think_blocks()` 剥离推理内容后写入历史 |
| 共享 client 不受污染 | 每次请求创建独立 client；陌生人线程只做 socket shutdown |
| 虚假网络错误不泄露 | `_request_cancelled` 标志让工作线程识别被中断的连接 |
| 会话持久化保障 | `agent._persist_session()` 在中断处理中立即调用 |
| 中断响应实时性 | 300ms 轮询间隔，最坏情况延迟 300ms |
