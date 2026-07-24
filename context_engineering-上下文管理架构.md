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

在 `compress()` 之上做会话管理，核心函数 `compress_context()`（~820 行）：

### `compress_context()` 整体骨架

```
compress_context(agent, messages, system_message)
│
├─ 0. Codex App Server 路由        → 特殊路径直接 return
│
├─ 1. 守门员检查                    → cooldown/breaker 拦截 → return (no-op)
│
├─ 2. 懒加载可行性检查               → 首次压缩才跑，省 ~400ms 冷启动
│
├─ 3. 准备工作                      → 读 in_place 标志、打日志、emit_status
│
├─ 4. 获取压缩锁                    → SQLite lock（三段决策）
│
├─ 5. 调用压缩引擎                  → compressor.compress() 返回 compressed
│
├─ 6. 拍质量快照（轮转回调前）        → 防止回调重置 per-session 字段
│
├─ 7. 失败路径                      → abort / no-change → return (no-op)
│
├─ 8. 压缩边界持久化（核心 try 块）   → commit_memory → in_place/rotation → SP
│
├─ 9. 边界通知                      → context_engine + memory_manager
│
├─ 10. 反抖动追踪                   → 记录效果、设置 cooldown
│
└─ 11. 释放锁 + return
```

核心就三步：**锁 → 压缩 → 持久化**，其余全是防御性代码。

---

### 1. 懒加载可行性检查（522-536行）

```python
if not getattr(agent, "_compression_feasibility_checked", False):
    check_compression_model_feasibility(agent)
    agent._compression_feasibility_checked = True
```

把压缩模型探测从 `AIAgent.__init__` 推迟到**第一次真正需要压缩时**。`check_compression_model_feasibility()` 会探测辅助 LLM API 可达性、上下文窗口大小、必要时调整压缩阈值。每次探测耗时约 **400ms**，但绝大多数短会话（`chat -q`）根本不会触发压缩，省掉这笔开销。

关键细节：flag **在检查完成后**才置 True 而非检查前——如果检查抛了致命异常，flag 保持 False，下个 session 还能重试，不会永久卡死。

---

### 2. 压缩前准备工作（538-555行）

```python
_pre_msg_count = len(messages)                                    # 记录压缩前消息数
in_place = bool(getattr(agent, "compression_in_place", False))    # 读配置
compacted_in_place = False               # 占位标志，DB 写入成功后才置 True
logger.info("context compression started: ...")                   # 打日志
agent._emit_status("🗜️ Compacting context...")   # 通知 UI 展示 "Summarizing…"
```

`compacted_in_place` 初始为 False，网关通过它区分「id 没变但被压缩了」（需要重新建立 transcript 基线）和「什么都没发生」。

---

### 3. 质量快照：在轮转回调之前捕获（736-745行）

```python
_compression_made_progress = bool(
    getattr(agent.context_compressor, "_last_compression_made_progress", False)
)
_compression_used_fallback = bool(
    getattr(agent.context_compressor, "_last_summary_fallback_used", False)
)
```

**必须在轮转/回调之前读取**。因为后续 `on_session_start()` 在 rebind 到子会话 ID 时，可能会重置 compressor 的 per-session 字段。如果先轮转再读，拿到的是重置后的 `False`，而非本次压缩的真实效果。这两个值用于后续的反抖动追踪——无效压缩多了触发拦截。

---

### 4. 两个 no-op 失败路径（747-781行）

**路径一：LLM 调用失败**（747-767行）

```python
if getattr(agent.context_compressor, "_last_compress_aborted", False):
    # 取错误信息，去重展示 warning
    # 返回原消息 → 不轮转、不持久化
    # finally 里释放锁
```

摘要模型超时/429/返回垃圾 → compressor 设 `_last_compress_aborted = True`，messages 原样返回。外层通过 `len(returned) == len(input)` 感知到 no-op，停止重试。

**路径二：无事可压**（768-781行）

```python
if compressed is messages:    # 用 is 而非 ==：比较对象身份
    # LLM 成功了，但中间区域为空，compressor 直接 return messages
    # 不轮转、不持久化
```

LLM 正常返回但**没有可压缩的中间内容**（比如对话本身就很短）。用 `is` 比较对象身份——如果 compressor 发现无事可压，直接把入参对象原样扔回来，连 copy 都没做。

两个路径的区别：路径一是「**调 LLM 失败了**」，路径二是「**调了 LLM 但没东西可压**」。结果相同——不轮转、不丢消息、释放锁。

---

### 5. 压缩边界持久化：核心 try 块（819-964行）

```python
if agent._session_db:
    try:
        # 步骤一：压缩前先提取记忆
        agent.commit_memory_session(messages)

        # 步骤二：分叉
        if in_place:    ...   # 原地替换
        else:           ...   # 轮转换代

        # 步骤三：共享收尾
        agent._session_db.update_system_prompt(session_id, new_system_prompt)
        agent._last_flushed_db_idx = 0
    except Exception as e:
        # 区分「已回滚」和「孤儿会话」两种情况
```

#### 步骤一：压缩前提取记忆

在改写 transcript 之前，先把完整对话送给 memory provider——不管 session_id 变不变，中间 turns 马上要被摘要覆盖，必须在此之前让记忆系统抽取关键信息。

#### 步骤二-A：原地压缩

```python
agent._session_db.archive_and_compact(session_id, compressed)
agent._flushed_db_message_ids = set()
compacted_in_place = True
```

- `archive_and_compact` — 原子事务：旧行软归档 + insert compressed
- `_flushed_db_message_ids = set()` — 重置去重标记。下一轮 compressed 里的 dict 通过 identity 被跳过，只有真正新增的 turn 才会 flush
- 这里**不预先 flush**——compressed 已包含被保护尾部的最新消息

#### 步骤二-B：轮转换代（详细）

```
原 session (abc123)
    ├─ flush 未持久化消息（best-effort，失败不阻塞）
    ├─ end_session("abc123", "compression")
    ├─ session_id = 时间戳_UUID   ← 新 ID
    ├─ 同步三件套：
    │   ├─ contextvar: set_current_session_id(new_id)
    │   ├─ env:        os.environ["HERMES_SESSION_ID"]
    │   └─ logging:    set_session_context(new_id)
    │       ↑ 两套独立机制，不同 try 保护（logging 失败不连坐 gateway 路由）
    ├─ create_session(new_id, parent=old_id)
    │   └─ 如果失败？→ 孤儿会话回滚（见下文）
    ├─ migrate_goal_to_session(old_id, new_id)  ← /goal 静默丢失 (#33618)
    └─ 标题自增："project" → "project #2" → "project #3"
```

#### 孤儿会话回滚机制（903-940行）

```python
except Exception as _cs_err:
    # 子会话创建失败 → agent 拿着新 ID 但 state.db 里没有对应行
    # → 新消息写入不存在的会话 → 孤儿 (#33906/#33907)
    
    agent.session_id = old_session_id           # 回滚到父 ID
    set_current_session_id(old_session_id)       # 回滚 contextvar
    os.environ["HERMES_SESSION_ID"] = old_id     # 回滚 env
    set_session_context(old_session_id)          # 回滚 logging
    agent._session_db.reopen_session(old_id)     # 恢复已 end 的父会话
    old_session_id = None                        # 标记「没发生轮转」
    agent._session_db_created = True             # 父会话行已存在，无需重建
    raise                                        # 跳过后续迁移
```

设计原则：宁可回滚到父会话继续用，也不创造无家可归的孤儿会话。

#### 步骤三：共享收尾 — 更新 system prompt

```python
agent._session_db.update_system_prompt(agent.session_id, new_system_prompt)
agent._last_flushed_db_idx = 0
```

压缩后 system prompt 是**重建过**的——`_build_system_prompt()` 会把压缩摘要嵌入 sp。必须写回 DB 保证 `/resume` 时一致。

> **为什么 sp 需要更新？** 正常对话中 sp 不变，但压缩边界处的 sp 被重建了：
> ```
> 压缩前 sp:  ## System + ## Current Task
> 压缩后 sp:  ## System + [CONTEXT COMPACTION — REFERENCE ONLY]
>             + ## Historical Task Snapshot
>             + ## Historical Pending User Asks + ## Current Task
> ```

#### except 块：区分两种失败

```python
except Exception as e:
    if locals().get("old_session_id") is None and not in_place:
        # 已经回滚到父会话 → 恢复路径，不是孤儿
        logger.warning("...rolled back to the parent session...")
    else:
        # 父已 end，子没创建 → 真正的孤儿
        logger.warning("...new session will NOT be indexed...")
```

`locals().get("old_session_id")` 的判断逻辑：回滚代码块 `raise` 之前把 `old_session_id = None`，所以通过这个变量是否存在判断是否已经完成了回滚。

---

### 自动压缩的守门员：`_automatic_compression_blocked()`

位于 [context_compressor.py:1379-1413](../agent/context_compressor.py#L1379)，每次自动压缩前都会被 `compress_context()` 调用（手动 `/compress` 传 `force=True` 跳过此检查）。

检查两个条件，任一命中即阻止本次压缩：

**条件一：摘要 LLM 处于冷却期**

```python
_cooldown_remaining = self._summary_failure_cooldown_until - time.monotonic()
if _cooldown_remaining > 0:
    return True   # 阻止
```

场景：摘要模型返回 429 限流或瞬时错误后，`_generate_summary()` 会设置冷却期。如果不拦截，token 仍超阈值 → 下轮再触发压缩 → 再次失败 → 无限循环，**CLI 看起来像卡死**（issue #11529）。

**条件二：重复压缩无效（反抖动）**

```python
if (self._ineffective_compression_count >= 2 
    or self._fallback_compression_streak >= 2):
    return True   # 阻止，建议用户 /new
```

连续两次压缩后上下文用量仍不健康 → 说明摘要已经解决不了问题，再压也是浪费，提示用户考虑 `/new` 开启新会话。

**调用处的版本兼容写法**（[conversation_compression.py:511-515](../agent/conversation_compression.py#L511)）：

```python
# 走 type(instance).__name__ 类属性查找而非实例方法调用
# 热更新后模块版本不一致时，方法不存在 → getattr 返回 None → 降级放行
blocked = getattr(
    type(agent.context_compressor),
    "_automatic_compression_blocked",
    None,
)
if callable(blocked) and blocked(agent.context_compressor):
    existing_prompt = getattr(agent, "_cached_system_prompt", None)
    if not existing_prompt:
        existing_prompt = agent._build_system_prompt(system_message)
    return messages, existing_prompt   # 返回原消息，不做任何压缩
```

### 防御性版本兼容模式

项目中大量使用 `getattr(type(obj), "method", None)` 而非 `obj.method()` 直接调用，这是因为 Hermes 支持 `hermes update` 热更新——进程长期运行期间模块可能被替换：

```
老实例 × 新类 → 方法不存在 → getattr 返回 None → 优雅降级
新实例 × 老类 → 方法不存在 → getattr 返回 None → 优雅降级
```

| 决策 | 场景 | 理由 |
|---|---|---|
| **fail-open**（降级放行） | 方法不存在（版本偏斜） | 压缩失败下轮可以重试，阻塞则用户无法继续对话 |
| **fail-closed**（抛错阻断） | 方法存在但执行异常 | 锁获取失败继续压缩会 fork session lineage |

### 压缩锁机（详细）

压缩锁的获取流程是一个**三段决策**，核心变量 `_lock_lookup_error` / `_try_acquire_lock` 组合出三种路径：

```
_lock_db.try_acquire_compression_lock 是否存在？
    │
    ├─ 查找过程抛异常 → _lock_lookup_error 非空
    │   → fail-closed：不安全，跳过本次压缩
    │
    ├─ 方法本身不存在（老版本 SessionDB）→ _try_acquire_lock is None
    │   → fail-open：版本偏斜不应阻塞压缩，无锁放行
    │
    └─ 方法存在 → 调用 try_acquire_compression_lock()
        ├─ 成功 → 执行压缩 → release_compression_lock()
        └─ 失败（实现层异常）→ fail-closed：释放残留锁，跳过本次压缩
```

```python
try_acquire_compression_lock(session_id, holder, ttl=300s)
    ├─ 获取成功 → 执行压缩 → release_compression_lock()
    └─ 获取失败 → 跳过本次压缩，返回原消息
```

持锁期间有 `_CompressionLockLeaseRefresher` 后台线程定期续约（默认间隔为 TTL 的一半）。

锁的 key 是**旧的 session_id**（轮转前的父会话 ID），因为并发竞争路径看到的都是这个旧 ID。

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
