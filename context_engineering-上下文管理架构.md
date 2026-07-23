# Context Engineering — 上下文管理架构

## 概述

Hermes Agent 的上下文管理系统解决一个问题：**当对话越来越长、逼近模型 token 上限时，怎么办？**

答案是一套**可插拔的 Context Engine 抽象层**，默认实现为**中间 turn 摘要压缩**。

---

## 一、架构分层

```
┌──────────────────────────────────────────────────┐
│  cli.py  /  run_agent.py  /  gateway             │  ← 上层调用者
├──────────────────────────────────────────────────┤
│  conversation_compression.py                     │  ← 会话级编排（锁、轮转、持久化）
├──────────────────────────────────────────────────┤
│  context_engine.py  (ContextEngine ABC)          │  ← 抽象接口层
├──────────────────────────────────────────────────┤
│  context_compressor.py  (ContextCompressor)      │  ← 默认实现（摘要压缩）
│  [第三方引擎: LCM / DAG / ...]                    │  ← 可插拔实现
├──────────────────────────────────────────────────┤
│  hermes_state.py  (SQLite SessionDB)             │  ← 持久化层
└──────────────────────────────────────────────────┘
```

---

## 二、context_engine.py — 抽象基类

**文件**: [agent/context_engine.py](../agent/context_engine.py)（232 行，纯抽象，无实现逻辑）

定义「上下文管理」这个关注点的可插拔接口。上层代码不关心具体策略。

### 核心接口（子类必须实现）

| 方法 | 职责 |
|---|---|
| `name` (property) | 引擎标识，如 `"compressor"`、`"lcm"` |
| `update_from_response(usage)` | 每次 LLM 调用后更新 token 计数 |
| `should_compress(prompt_tokens)` | 判断当前是否需要触发压缩 |
| `compress(messages, current_tokens, focus_topic)` | **核心入口** — 接收完整消息列表，返回压缩后的消息列表 |

### 完整生命周期

```
实例化 → on_session_start()
            ↓
        update_from_response()   ← 每轮 LLM 调用后
            ↓
        should_compress()        ← 每轮检查
            ↓ (返回 True)
        compress()               ← 执行压缩
            ↓
        ... 循环 ...
            ↓
        on_session_end()         ← CLI 退出 / /reset / gateway 过期
```

### 可选钩子

| 方法 | 用途 |
|---|---|
| `should_compress_preflight()` | API 调用**前**的粗略预估，默认返回 False |
| `has_content_to_compress()` | `/compress` 命令前置检查，避免无内容时发起 LLM 调用 |
| `on_session_reset()` | `/new` 或 `/reset` 时重置计数器 |
| `get_tool_schemas()` | 引擎可提供自己的工具给 agent 调用（如 LCM 的 `lcm_grep`） |
| `handle_tool_call()` | 处理 agent 对引擎工具的调用 |
| `get_status()` | 返回使用率、压缩次数等状态信息 |
| `update_model()` | 用户切换模型时重新计算 threshold |

### 默认压缩参数

| 参数 | 默认值 | 含义 |
|---|---|---|
| `threshold_percent` | `0.75` | 上下文用量达到 75% 触发 |
| `protect_first_n` | `3` | 始终保留前 3 条非系统消息（system prompt 始终保护） |
| `protect_last_n` | `6` | 始终保留最后 6 条消息 |

### 关键状态字段（run_agent.py 直接读取）

```python
last_prompt_tokens: int     # 最近一次 prompt token 数
last_completion_tokens: int # 最近一次输出 token 数
last_total_tokens: int      # 最近一次总 token 数
threshold_tokens: int       # 触发压缩阈值 = context_length * threshold_percent
context_length: int         # 模型上下文窗口大小
compression_count: int      # 累计压缩次数
```

### 引擎选择

通过 `config.yaml` 的 `context.engine` 字段切换，或放入 `plugins/context_engine/<name>/` 目录。

---

## 三、context_compressor.py — 默认压缩引擎

**文件**: [agent/context_compressor.py](../agent/context_compressor.py)

`ContextCompressor(ContextEngine)` — 通过**有损摘要**来压缩对话上下文。

### 算法流程

```
1. 裁剪旧工具结果 (Prune Tool Results)
   └─ 将旧的 tool result 替换为一行摘要（无 LLM 调用，低成本）

2. 保护头部 (Protect Head)
   └─ System prompt + 前 protect_first_n 条非系统消息

3. 按 Token 预算保护尾部 (Protect Tail)
   └─ 从末尾向前累计约 20K tokens，确保最后一条 user/assistant 在尾部

4. 对中间 turns 做 LLM 摘要 (Summarize Middle)
   └─ 调用辅助模型（如 Gemini Flash），结构化的摘要模板

5. 迭代更新摘要 (Iterative Update)
   └─ 再次压缩时增量更新已有摘要，而非从头总结
```

### 摘要模板结构

- `HISTORICAL_TASK_HEADING` — "## Historical Task Snapshot"
- `HISTORICAL_IN_PROGRESS_HEADING` — "## Historical In-Progress State"
- `HISTORICAL_PENDING_ASKS_HEADING` — "## Historical Pending User Asks"
- `HISTORICAL_REMAINING_WORK_HEADING` — "## Historical Remaining Work"

摘要前缀明确标注为 `[CONTEXT COMPACTION — REFERENCE ONLY]`，告诉模型这些内容仅供背景参考，不是活跃指令。

### 反抖动机制

- **冷却期 (cooldown)** — 压缩失败后一段时间内不重试
- **有效性追踪** — 跟踪压缩是否有实际效果
- **回退连击 (fallback streak)** — 防止无限压缩循环

---

## 四、conversation_compression.py — 会话级编排

**文件**: [agent/conversation_compression.py](../agent/conversation_compression.py)

在 `compress()` 之上做会话管理，核心函数 `compress_context()`：

### 职责

1. **可行性检查** — 启动时探测辅助压缩模型是否可用
2. **压缩锁** — 基于 SQLite 的分布式锁，防止并发压缩同一会话导致 session fork
3. **调用压缩引擎** — 执行实际的 LLM 摘要
4. **持久化** — 将压缩结果写入 SQLite
5. **通知插件** — 通知 context engine 和 memory provider 发生了压缩边界

### 压缩锁机制

```
try_acquire_compression_lock(session_id, holder, ttl=300s)
    ├─ 获取成功 → 执行压缩 → release_compression_lock()
    └─ 获取失败 → 跳过本次压缩，返回原消息
```

持锁期间有 `_CompressionLockLeaseRefresher` 后台线程定期续约。

---

## 五、持久化机制

### 压缩结果一定会写入 SQLite

无论哪种模式，压缩后的摘要 + 尾部消息都会作为新的 `active=1` 行持久化。

### 模式一：轮转模式（Legacy Rotation）

```
压缩前                            压缩后
┌──────────────────┐            ┌──────────────────┐
│ session_id: abc  │            │ session_id: abc   │
│ (原始完整对话)     │  ──→       │ end_session()     │
│                  │            │ 原始消息保留不动    │
└──────────────────┘            └──────────────────┘
                                        │ parent_session_id
                                        ▼
                                ┌─────────────────────────┐
                                │ session_id: 20260723_…  │
                                │ (摘要 + 保护尾部)         │
                                │ 写入新 active 行         │
                                └─────────────────────────┘
```

### 模式二：原地压缩（In-Place, `compression.in_place: true`）

```
压缩前                               压缩后（同一个 session_id）
┌─────────────────────────┐        ┌──────────────────────────────┐
│ messages 表              │        │ messages 表                   │
│  row1: active=1 原始     │        │  row1: active=0, compacted=1  │ ← 软归档
│  row2: active=1 原始     │  ──→   │  row2: active=0, compacted=1  │ ← 软归档
│  row3: active=1 原始     │        │  row3: active=0, compacted=1  │ ← 软归档
│  row4: active=1 原始     │        │  row4: active=1 (摘要消息)     │ ← 新写入
│                          │        │  row5: active=1 (保护尾部)     │ ← 新写入
└─────────────────────────┘        └──────────────────────────────┘
```

关键代码在 [hermes_state.py:4010-4060](../hermes_state.py)，`archive_and_compact()` 在一个事务中原子完成：

```python
# 1. 软归档所有活跃行
UPDATE messages SET active = 0, compacted = 1
WHERE session_id = ? AND active = 1

# 2. 插入压缩后消息
INSERT INTO messages (...) VALUES (...)  -- compacted_messages

# 3. 更新计数
UPDATE sessions SET message_count = ? WHERE id = ?
```

| | 轮转模式 | 原地压缩 |
|---|---|---|
| session_id | **变了**（新 UUID） | **不变** |
| 原始消息 | 留在旧 session，active=1 | 同 session，active=0, compacted=1 |
| 压缩消息 | 写入新 session，active=1 | 同 session，active=1 |
| 全文搜索 | 可搜旧 session | 可搜 compacted=1 的行 |
| 可恢复 | 旧 session 可独立打开 | `include_inactive=True` 可捞回 |

---

## 六、其他相关文件

| 文件 | 作用 |
|---|---|
| [trajectory_compressor.py](../trajectory_compressor.py) | 离线/批量轨迹压缩（处理已完成的 JSONL 轨迹） |
| [hermes_cli/partial_compress.py](../hermes_cli/partial_compress.py) | `/compress here [N]` 边界感知部分压缩 |
| [hermes_cli/session_recap.py](../hermes_cli/session_recap.py) | `/recap` 只读摘要，本地计算无 LLM 调用 |
| [agent/manual_compression_feedback.py](../agent/manual_compression_feedback.py) | 手动压缩后的用户反馈信息 |

---

## 七、设计上的扩展点

文档中提到但未实现的策略方向：

- **DAG 结构** — 不靠有损摘要，而用有向无环图组织上下文依赖关系，按需检索相关节点而非丢弃中间内容
- **LCM (Large Context Model)** — 构建可检索的知识图谱，上下文不足时从图中捞取
