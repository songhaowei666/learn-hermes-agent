# 记忆系统双路径架构 — 前台Tool调用 + 后台Review Fork

> 涉及文件：[agent/agent_init.py](agent/agent_init.py#L1370-L1374)、[tools/memory_tool.py](tools/memory_tool.py)、[agent/background_review.py](agent/background_review.py)、[agent/turn_finalizer.py](agent/turn_finalizer.py#L500-L508)

## 概述

Hermes Agent 的记忆更新有**两条互补路径**：

1. **前台 Tool 调用**：LLM 在主对话中通过 `memory` 工具自主决策写入
2. **后台 Review Fork**：每轮对话结束后，fork 独立 Agent 在后台 daemon 线程中回顾整个对话，自动更新记忆和 Skill

两条路径共享同一个 `agent._memory_store`（`MemoryStore` 实例），都写入 `~/.hermes/memories/MEMORY.md` 和 `USER.md`。

---

## 一、MemoryStore — 文件持久化记忆存储

### 初始化（[agent_init.py:1370-1374]）

```python
from tools.memory_tool import MemoryStore
agent._memory_store = MemoryStore(
    memory_char_limit=mem_config.get("memory_char_limit", 2200),
    user_char_limit=mem_config.get("user_char_limit", 1375),
)
```

### 两份文件

| 文件 | 内容 | 默认字符上限 |
|---|---|---|
| `~/.hermes/memories/MEMORY.md` | Agent 的个人笔记（环境事实、项目约定、工具怪癖、学到的东西） | 2200 |
| `~/.hermes/memories/USER.md` | 用户档案（偏好、风格、期望、工作习惯） | 1375 |

### 冻结快照模式（核心设计精髓）

```
Session 启动
    │
    ▼
load_from_disk()  →  _system_prompt_snapshot  →  注入系统提示词（永不改变）
    │                                             保持 prefix cache 稳定
    ▼
mid-session 写入 →  live entries 更新  →  写磁盘  →  snapshot 不变
    │
    ▼
下次 Session  →  load_from_disk()  →  新 snapshot = 上次写入内容
```

**为什么不能 mid-session 改系统提示词？** 变了 prefix cache 就失效，每轮都要重发全部 token。

### 操作 API

| 方法 | 说明 |
|---|---|
| `add(target, content)` | 追加条目，超过 char limit 拒绝 |
| `replace(target, old_text, new_content)` | 子字符串匹配替换 |
| `remove(target, old_text)` | 子字符串匹配删除 |
| `apply_batch(target, operations)` | **批量原子操作**（add + replace + remove 一次完成，只校验最终结果） |
| `load_from_disk()` | Session 启动时加载 |
| `format_for_system_prompt(target)` | 返回冻结快照（非 live state） |

### 安全机制

| 机制 | 说明 |
|---|---|
| 威胁模式扫描 | 所有新内容经过 `scan_for_threats(scope="strict")` 扫描 |
| 外部漂移检测 | 检查文件是否被外部手动编辑，发现则拒绝覆盖并保存 `.bak` |
| 文件锁 | `fcntl`/`msvcrt` 锁，防多 session 并发写冲突 |
| 原子写 | temp file + `os.replace` rename，读者要么读到旧完整文件要么新完整文件 |
| 容量限制 | `MAX_TODO_CONTENT_CHARS=4000`, `MAX_TODO_ITEMS=256`, `MAX_TODO_RESULT_CHARS=512000` |
| 合并失败保护 | 同 turn 最多 3 次合并失败后终止（`_MAX_CONSOLIDATION_FAILURES_PER_TURN=3`），防止死循环耗尽预算 |

---

## 二、前台 Tool 调用路径

### 工具注册（[memory_tool.py:1132-1148]）

```python
registry.register(
    name="memory",
    toolset="memory",
    schema=MEMORY_SCHEMA,
    handler=lambda args, **kw: memory_tool(
        action=args.get("action", ""),
        target=args.get("target", "memory"),
        content=args.get("content"),
        old_text=args.get("old_text"),
        operations=args.get("operations"),
        store=kw.get("store")),  # ← 传入 agent._memory_store
    check_fn=check_memory_requirements,
    emoji="🧠",
)
```

### 工具执行（[tool_executor.py:1236-1242]）

```python
elif function_name == "memory":
    result = memory_tool(
        action=args.get("action"),
        target=args.get("target", "memory"),
        content=args.get("content"),
        old_text=args.get("old_text"),
        operations=args.get("operations"),
        store=agent._memory_store,  # ← 传入 agent 的 store 实例
    )
```

### LLM 何时调用

Schema 描述中定义（[memory_tool.py:1076-1085]）：

- **WHEN**: 用户声明偏好/纠正/个人信息时，或学到稳定环境事实/约定/工作流时
- **优先级**: 用户偏好和纠正 > 环境事实 > 操作流程
- **SKIP**: 琐碎/显而易见的信息、可轻松重新发现的事实、原始数据转储、任务进度、已完成工作日志

### Write Approval Gate

记忆写入可能经过 `write_approval` 门控（`_apply_write_gate`），支持三种结果：
- **allow**: 直接写入
- **block**: 拒绝
- **stage**: 暂存等待审批（gateway/后台模式）

---

## 三、后台 Review Fork 路径

这是用户记忆中 "fork 一个 agent 去更新记忆和 skill" 的核心机制。

### 触发条件（[turn_finalizer.py:500-508]）

```python
# 在 response 已返回给用户后才执行，不竞争模型注意力
if final_response and not interrupted and (_should_review_memory or _should_review_skills):
    agent._spawn_background_review(
        messages_snapshot=list(messages),
        review_memory=_should_review_memory,
        review_skills=_should_review_skills,
    )
```

触发条件基于 **nudge interval**（默认每 ~10 个 tool-iterations）：
- `_should_review_memory`: `agent._iters_since_memory >= agent._memory_nudge_interval`
- `_should_review_skills`: `agent._iters_since_skill >= agent._skill_nudge_interval`

### Fork Agent 创建（[background_review.py:699-794]）

```python
review_agent = AIAgent(
    model=_rt.get("model") or agent.model,
    max_iterations=16,
    quiet_mode=True,
    # ... 继承父 agent 的 provider/model/base_url/api_key
    parent_session_id=agent.session_id,
    skip_memory=True,  # 不碰外部记忆插件（honcho, mem0等）
)
```

### 关键配置

| 配置 | 说明 |
|---|---|
| `review_agent._memory_store = agent._memory_store` | **共享父 agent 的 store**，写入直接落地到同一文件 |
| `review_agent._cached_system_prompt = agent._cached_system_prompt` | 复用父 agent 系统提示词，命中 prefix cache |
| `review_agent._persist_disabled = True` | **不污染用户 session DB**（curator-takeover 根因修复） |
| `review_agent.compression_enabled = False` | review 需要完整上下文 |
| `review_agent.suppress_status_output = True` | 静默运行 |
| `review_agent._skip_mcp_refresh = True` | 跳过 MCP 刷新，保持 `tools[]` 与父 agent 一致保 cache |
| `review_agent._end_session_on_close = False` | 不关闭父 agent 的 session |

### 工具白名单

```python
review_toolsets = ["skills"]
if review_agent._memory_enabled or review_agent._user_profile_enabled:
    review_toolsets.insert(0, "memory")

review_whitelist = {
    t["function"]["name"]
    for t in get_tool_definitions(enabled_toolsets=review_toolsets)
}
set_thread_tool_whitelist(review_whitelist, ...)
```

仅允许 `memory` 和 `skill_manage` 工具，其他工具运行时拒绝。

### 危险命令自动拒绝

```python
def _bg_review_auto_deny(command, description, **kwargs):
    logger.warning("...")
    return "deny"

_set_approval_callback(_bg_review_auto_deny)
```

防止后台线程因危险命令审批弹窗 `input()` 与父进程 TUI 死锁（issue #15216）。

### Review Prompt

三个 prompt 按触发类型选择：
- **仅 memory**: `_MEMORY_REVIEW_PROMPT`（[background_review.py:170-179]）
- **仅 skills**: `_SKILL_REVIEW_PROMPT`（[background_review.py:181-283]）
- **两者都触发**: `_COMBINED_REVIEW_PROMPT`（[background_review.py:286-369]）

所有 prompt 都以 "Nothing to save." 作为合法输出，不强制造假。

### 路由优化

- **同模型**（default）：完整对话快照重放，warm cache reads
- **不同模型**（`auxiliary.background_review.{provider,model}` 配置）：发送压缩摘要（`_digest_history`），减少 cold-write token

### 结果汇总

`summarize_background_review_actions()` 扫描 review agent 的 tool 调用结果，过滤掉已在 `messages_snapshot` 中的旧结果（issue #14944），生成用户可见的摘要行：

```
💾 Self-improvement review: Memory ➕ prefers tabs over spaces · 📝 Skill 'python-dev' patched
```

通过 `background_review_callback` 或 `_safe_print` 展示给用户。

---

## 四、与 TodoStore 的对比

| | TodoStore | MemoryStore |
|---|---|---|
| **存储** | 纯内存，session 结束消失 | **文件持久化** `~/.hermes/memories/` |
| **跨 session** | 否 | 是，下次启动自动加载 |
| **系统提示词** | 不涉及 | 注入冻结快照（启动时确定） |
| **后台 fork** | 否 | 是（background_review） |
| **并发安全** | 不需要 | 文件锁 + 原子写 |
| **威胁扫描** | 无 | 有 |
| **Write gate** | 无 | 有（write_approval） |
| **外部漂移检测** | 无 | 有 |

---

## 五、调用链路

```
主对话中                              turn 结束后
────────                             ──────────

LLM 判断需要记住某事                   turn_finalizer.finalize_turn()
    │                                      │
    ▼                                      ▼
调用 memory(action="add", ...)         _spawn_background_review()
    │                                      │
    ▼                                      ▼
tool_executor.py                     background_review.py
→ memory_tool(store=agent._memory_store)  → fork AIAgent
    │                                      │
    ▼                                      ▼
MemoryStore.add()                     review_agent.run_conversation()
    ├── 威胁扫描                           │
    ├── 文件锁                             ▼
    ├── 容量检查                      review prompt + full/digest history
    ├── 写磁盘（原子 replace）              │
    └── 返回结果                           ▼
                                      LLM 自主判断 + 调用 memory/skill_manage
                                          │
                                          ▼
                                      summarize_background_review_actions()
                                          │
                                          ▼
                                      用户看到 "💾 Self-improvement review: ..."
```
