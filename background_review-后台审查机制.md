# background_review — 后台记忆/技能审查机制

> 完整追踪路径：`turn_finalizer.py` → `run_agent.py._spawn_background_review` → `background_review.py.spawn_background_review_thread` → `_run_review_in_thread`

## 一、触发机制：两个独立的 Nudge 计数器

```
┌──────────────────────────────┬──────────────────────────────┐
│   _memory_nudge_interval     │   _skill_nudge_interval      │
│   默认值: 10 (user turns)    │   默认值: 10 (tool iterations)│
│   计数器: _turns_since_memory │   计数器: _iters_since_skill  │
│   递增点: turn_context.py    │   递增点: conversation_loop.py│
│   (每个 user turn +1)        │   (每次工具调用 +1)           │
│   配置: memory.nudge_interval│   配置: skills.creation_      │
│                              │   nudge_interval              │
├──────────────────────────────┴──────────────────────────────┤
│   达到阈值 → turn_finalizer.py:502 触发后台审查              │
│   review_agent 上两个 interval 都设为 0 → 防止递归           │
└─────────────────────────────────────────────────────────────┘
```

### 触发条件（三选一）

```python
# background_review.py:958-963
if review_memory and review_skills:    # 两个都触发
    prompt = _COMBINED_REVIEW_PROMPT    # 合并审查
elif review_memory:                     # 只触发 memory
    prompt = _MEMORY_REVIEW_PROMPT       # 纯记忆审查
else:                                   # 只触发 skill
    prompt = _SKILL_REVIEW_PROMPT        # 纯技能审查
```

- `_turns_since_memory` 持久化：session resume 时从历史消息数还原，跨网关重启不丢

### 直接调用工具也会重置计数器

```python
# tool_executor.py:378-381
if function_name == "memory":
    agent._turns_since_memory = 0
elif function_name == "skill_manage":
    agent._iters_since_skill = 0
```

---

## 二、审查 Fork 的创建

`background_review.py` 中 `_run_review_in_thread` 创建 `AIAgent` 子实例：

### 两层设计：请求层统一 + 调度层限制

```
┌─ HTTP 请求层（为了缓存命中）──────────────────┐
│  enabled_toolsets = parent 的 toolsets         │  ← 711行
│  disabled_toolsets = parent 的 toolsets         │  ← 712行
│  _cached_system_prompt = parent 的 system prompt│  ← 767行
│  session_id = parent 的 session_id              │  ← 776行
│                                                 │
│  效果：tools[] + system 跟 parent 一模一样      │
│  → Anthropic prefix cache HIT → 省 26% 成本     │
├─────────────────────────────────────────────────┤
│ 运行时调度层（安全拦截）                         │
│  set_thread_tool_whitelist({"memory","skill"})  │  ← 816行
│  → 模型调 bash/read/write 等 → 返回 error JSON  │
└─────────────────────────────────────────────────┘
```

### 三个 Prompts 的来源

```python
# background_review.py:170-369 — 模块级常量（真正的内容）
_MEMORY_REVIEW_PROMPT = "Review the conversation above..."   # ~10行
_SKILL_REVIEW_PROMPT = "Review the conversation above..."    # ~100行
_COMBINED_REVIEW_PROMPT = "Review the conversation above..." # ~80行

# run_agent.py:1580-1584 — class body 级别 import → 变成 AIAgent 类属性
class AIAgent:
    from agent.background_review import (
        _MEMORY_REVIEW_PROMPT, _SKILL_REVIEW_PROMPT, _COMBINED_REVIEW_PROMPT
    )

# background_review.py:958-963 — 选择 prompt（getattr 允许实例覆盖）
prompt = getattr(agent, "_COMBINED_REVIEW_PROMPT", _COMBINED_REVIEW_PROMPT)
```

`getattr` 设计允许测试在实例上设短 prompt（`agent._MEMORY_REVIEW_PROMPT = "review memory"`）而不需要 mock 模块常量。

---

## 三、运行时白名单拦截链

```
模型尝试调用 bash("rm -rf /")
  │
  ▼
model_tools.py:1185
  resolve_pre_tool_block("bash", ...)
  │
  ▼
plugins.py:2252
  _get_pre_tool_call_directive_details("bash", ...)
  │
  ▼
plugins.py:2137-2143
  allowed = _thread_tool_whitelist.allowed  # {"memory", "skill_manage"}
  "bash" not in {"memory", "skill_manage"}? → YES
  │
  ▼
  return _PreToolCallDirective(action="block", message="...denied...")
  │
  ▼
plugins.py:2257
  action == "block" → return message  # 正常 return，不是 raise
  │
  ▼
model_tools.py:1198-1214
  block_message 非空 → return json.dumps({"error": message})
  │
  ▼
模型收到: {"error": "Background review denied non-whitelisted tool: bash..."}
  → 模型看到 error 字符串，知道换 memory/skill 工具重试
```

**关键设计**：全链路用 `return` 不用 `raise`。`_PreToolCallDirective` 是 dataclass 不是异常。工具函数从头到尾没被调用——在前置检查就被短路了。

---

## 四、Prompt Cache 机制

被缓存的不是审查提示词，而是 **system prompt + tools 定义**。

### 正常聊天时 cache 的内容

```
POST /v1/messages
┌────────────────────────────────┐
│ system: "You are Hermes..."    │  ← 前缀缓存命中
│ tools: [{memory}, {bash}...]   │  ← 前缀缓存命中
├────────────────────────────────┤  ↑ Anthropic prefix cache
│ messages: [                    │  ← 不缓存（每次都不同）
│   {user: "帮我写脚本"},         │
│   {assistant: "..."},          │
│ ]                              │
└────────────────────────────────┘
```

### 审查 fork 如何蹭缓存

| 操作 | 代码位置 | 作用 |
|------|---------|------|
| `enabled_toolsets` 跟 parent 一致 | 711行 | `tools[]` 字节相同 → cache key 命中 |
| `_cached_system_prompt` 复用 | 767行 | system 字节相同 → cache key 命中 |
| `session_id` 设成 parent 的 | 776行 | 同一个 session → cache 命中 |
| `session_start` 设成 parent 的 | 775行 | 防止重新生成时间戳破坏字节一致性 |

`_MEMORY_REVIEW_PROMPT` 是塞进 `messages[]` 的 user message，很短（几百 token），不需要也没法缓存。

---

## 五、防护措施

| 防护 | 代码位置 | 目的 |
|------|---------|------|
| `_memory_nudge_interval = 0` | 728行 | 防止审查 fork 再触发 memory 审查（递归） |
| `_skill_nudge_interval = 0` | 729行 | 防止审查 fork 再触发 skill 审查（递归） |
| `_persist_disabled = True` | 741行 | 防止审查消息写入用户真实 session DB |
| `_end_session_on_close = False` | 783行 | 防止 close() 终结 parent 的 session |
| `compression_enabled = False` | 794行 | 防止审查 fork 对 parent session 做压缩 |
| `skip_memory = True` | 713行 | 防止审查 fork 连接外部 memory 插件（honcho/mem0 等） |
| `suppress_status_output = True` | 751行 | 防止审查中间日志泄漏到用户终端 |
| `_skip_mcp_refresh = True` | 724行 | 防止 MCP 工具延迟加入 + 破坏 tools[] 一致性 |

---

## 六、完整生命周期

```
用户 turn 开始
  │
  ▼
turn_context.py (pre-loop)
  _turns_since_memory += 1
  │
  ▼
conversation_loop.py
  每次 tool iteration → _iters_since_skill += 1
  │
  ▼
turn_finalizer.py (post-response)
  ├─ _turns_since_memory >= 10? → should_review_memory = True
  ├─ _iters_since_skill >= 10?  → should_review_skills = True
  └─ should_review_memory or should_review_skills?
       │
       YES → _spawn_background_review()
       │
       ▼
     background_review.py
       1. fork AIAgent (max_iterations=16)
       2. 继承 toolsets、system prompt、session_id
       3. 设白名单 {memory, skill_manage}
       4. 设所有防护 flag (防递归、防持久化、防压缩、防输出)
       5. run_conversation(prompt)
       6. 返回 → 提取 action summary 展示给用户
       7. close() + clear_thread_tool_whitelist()
```

## 七、核心文件索引

| 文件 | 职责 |
|------|------|
| `agent/agent_init.py:1360-1458` | 初始化 interval 默认值，读取配置覆盖 |
| `agent/turn_context.py:285-313` | 每个 turn 递增 memory 计数器，检查触发 |
| `agent/conversation_loop.py:700-702` | 每次迭代递增 skill 计数器 |
| `agent/turn_finalizer.py:484-510` | 检查 skill+memory 触发，调用 spawn |
| `agent/background_review.py:643-868` | fork 创建、防护设置、白名单、run |
| `agent/background_review.py:943-968` | prompt 选择逻辑 |
| `agent/background_review.py:170-369` | 三个审查 prompt 文本 |
| `hermes_cli/plugins.py:2079-2143` | 线程级白名单存储与检查 |
| `model_tools.py:1181-1214` | 工具调度层的前置拦截 |
| `run_agent.py:393` | AIAgent 类定义 |
| `run_agent.py:1580-1584` | class body import → 类属性 |
| `run_agent.py:1600-1624` | `_spawn_background_review` 包装 |
