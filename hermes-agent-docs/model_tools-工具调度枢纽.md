# model_tools.py — 工具调度枢纽

> 文件位置：`model_tools.py`（1381 行）
> 分析日期：2026-07-22

---

## 一、模块定位

`model_tools.py` 是 Hermes 的**工具调度薄层**。它自己不定义任何工具——工具 schema 和 handler 分散在 `tools/*.py` 各自注册——它的职责是两件事：

1. **向上**给 `AIAgent` 产出发给 LLM 的 `tools[]` 数组（`get_tool_definitions`）
2. **向下**把 LLM 返回的 `tool_use` 分发到正确的 handler 并拿到结果（`handle_function_call`）

一句话概括：**连接「LLM 想调工具」→「Python 函数被执行」→「结果塞回消息列表」的完整路径。**

---

## 二、入口方法速查

| 方法 | 被谁调用 | 用途 |
|---|---|---|
| `get_tool_definitions()` | AIAgent 初始化、配置变更时 | 产出 OpenAI 格式的 `tools[]` 数组 |
| `handle_function_call()` | `run_agent.py` 的对话循环、curator/background_review fork | 接收 LLM 的 function call，执行并返回 JSON 结果 |
| `coerce_tool_args()` | `handle_function_call` 开头、`run_agent.py._invoke_tool` | 把 LLM 的 `"42"` 修成 `42` |
| 4 个 `get_*` wrapper | `batch_runner.py`、`cli.py`、`doctor.py` | 向后兼容的查询接口 |

---

## 三、核心方法一：`get_tool_definitions()` — 第 279-354 行

### 3.1 调用链路

```
AIAgent.__init__()
  → get_tool_definitions(enabled_toolsets, disabled_toolsets, quiet_mode=True)
    → [缓存命中?] 直接返回
    → _compute_tool_definitions()  ← 真正干活
```

每次 agent 初始化、每次 toolset 变更时都会调用。Gateway 每收到一条用户消息都可能创建新 session → 新的 AIAgent → 新调用。所以这个方法被高度优化：**quiet_mode 路径带 memo 缓存**。

### 3.2 缓存策略

```python
cache_key = (
    frozenset(enabled_toolsets),
    frozenset(disabled_toolsets),
    registry._generation,        # registry 突变时自增，自动失效
    (config_mtime_ns, size),     # 配置文件改动时失效
    bool(HERMES_KANBAN_TASK),    # kanban worker 有额外 toolset
    bool(skip_tool_search_assembly),
)
```

- **LRU 上限 8 槽**：Gateway 长期运行会看到很多 toolset 组合，不设上限会内存泄漏
- **只缓存 quiet_mode=True 的结果**：quiet_mode=False 有 stdout 副作用，不能缓存
- **返回 shallow copy**：防止调用方 mutate 了缓存里的 dict 引用

### 3.3 `_compute_tool_definitions()` 七步流程 — 第 357-567 行

```
Step 1: 解析 enabled_toolsets → tools_to_include (set)
         ├─ enabled_toolsets 非空 → 逐个 resolve_toolset，取并集
         ├─ enabled_toolsets 为空 → 取所有 toolset 的并集（默认全量）
         └─ HERMES_KANBAN_TASK 环境变量 → 强制 +kanban

Step 2: 应用 disabled_toolsets → 从 tools_to_include 中减去
         ├─ platform bundle (hermes-*) → 只删非 core 工具，保核心
         └─ 普通 toolset → 整组删除

Step 3: registry.get_definitions(tools_to_include)
         → 每个工具过 check_fn，不可用的剔除

Step 4: 动态 schema 修正
         ├─ execute_code → 只列实际可用的 sandbox 工具
         ├─ discord/discord_admin → 按 bot intents 裁剪
         └─ browser_navigate → web_search 不可用时删描述中的交叉引用

Step 5: schema_sanitizer → 兼容 llama.cpp 的 GBNF parser

Step 6: Tool Search assembly → 非核心工具超阈值时替换为 bridge 工具

Step 7: 更新 _last_resolved_tool_names → 返回
```

关键设计决策在 Step 4：**工具的 schema 不是静态的**。`execute_code` 的描述里写着「你可以在 sandbox 里用 web_search」，但如果 web_search 对应的 toolset 被禁了，这段话必须从描述里删掉，否则模型会幻觉调用不存在的工具。

---

## 四、核心方法二：`handle_function_call()` — 第 1025-1352 行

这是文件的灵魂。LLM 返回 `tool_use` 后，`run_agent.py` 调这个函数。134 行代码，但路径分支很多：

```
handle_function_call(name, args, ...)
  │
  ├─ Step 0: coerce_tool_args(name, args)  ← 类型修正
  │
  ├─ Step 1: Tool Search bridge?  ← tool_search/tool_describe/tool_call
  │   ├─ tool_search  → 返回可搜索工具的目录
  │   ├─ tool_describe → 返回指定工具的完整 schema
  │   └─ tool_call    → 解包出真实 tool name+args，递归调用自己
  │
  ├─ Step 2: tool_request middleware  ← 插件可修改 args
  │
  ├─ Step 3: _AGENT_LOOP_TOOLS 检查  ← todo/memory/session_search/delegate_task
  │   这些工具不在这调度——返回 error，由 run_agent.py 的对话循环直接处理
  │
  ├─ Step 4: pre_tool_call hook → resolve_pre_tool_block()
  │   ├─ block → 返回 {"error": "..."}，不执行
  │   └─ 放行 → 继续
  │   （这就是 background_review 白名单生效的地方！）
  │
  ├─ Step 5: ACP/Zed edit approval → 文件写入前确认
  │
  ├─ Step 6: notify_other_tool_call() → 重置 read_file 的连续计数器
  │
  ├─ Step 7: registry.dispatch(name, args) → 真正调用 handler！
  │   外层包了 run_tool_execution_middleware（超时控制等）
  │
  ├─ Step 8: post_tool_call hook → 观察者插件收到通知
  │
  ├─ Step 9: transform_tool_result hook → 插件可改写返回值
  │
  └─ Step 10: return result (JSON string)
```

### 4.1 关键设计点

**Tool Search bridge 递归调用（Step 1 → tool_call）**：当 LLM 通过 `tool_call` 间接调用一个被 defer 的工具时，`handle_function_call` 解包出真实名称后**递归调用自己**。这样 Step 4-9 的所有 hook、middleware、安全检查对 bridge 完全透明——它们看到的永远是真实 tool name。

**`_AGENT_LOOP_TOOLS` 短路（Step 3）**：`todo`、`memory`、`session_search`、`delegate_task` 这四个工具需要读写 Agent 实例的内部状态（TodoStore、MemoryStore），不属于通用工具调度范畴。这是职责边界的清晰切割。

**pre_tool_call hook 的 `skip` 参数**：`run_agent.py._invoke_tool` 传 `skip_pre_tool_call_hook=True`，因为它自己已经调过一次 `resolve_pre_tool_block`。这里用这个 flag 防止同一个 tool call 触发两次 pre hook。

---

## 五、Async Bridging — 第 42-181 行

工具 handler 有同步有异步。这个模块需要把 async handler 在同步的对话循环里跑起来。

### 三级策略

```
_run_async(coro)
  ├─ 有 running loop? → ThreadPoolExecutor 开新线程，run_until_complete
  │   （Gateway/Atropos 的 async 栈内不能嵌套 run_until_complete）
  │
  ├─ 在 worker 线程? → per-thread persistent loop
  │   （避免 asyncio.run() 反复创建销毁 loop → httpx client 绑到死 loop）
  │
  └─ CLI 主线程 → persistent _tool_loop
      （同理，避免 "Event loop is closed"）
```

核心痛点：`asyncio.run()` 创建 loop、跑完、关闭 loop。但 `httpx.AsyncClient` 在 GC 时想用原来的 loop 清理连接——loop 已经关了 → RuntimeError。所以用 persistent loop，让 client 生命周期内 loop 一直活着。

---

## 六、Tool Arg Coercion — 第 652-961 行

LLM（尤其是开源模型）经常返回类型错误的参数。这套完整的类型修正管线在 `handle_function_call` 的 Step 0 执行：

```python
function_args = coerce_tool_args(function_name, function_args)
```

### 修正的内容

| 问题 | 示例 | 修正 |
|---|---|---|
| 数字变字符串 | `"max_results": "10"` | → `10` |
| 布尔变字符串 | `"recursive": "true"` | → `True` |
| 数组元素被 JSON-encode | `"todos": ["{\"id\":\"1\"}"]` | → `[{"id": "1"}]` |
| 裸标量不是数组 | `"urls": "https://a.com"` | → `["https://a.com"]` |
| null 字符串 | `"path": "null"` | → `None`（仅 schema 允许 null） |

### 为什么需要递归修正（`_normalize_json_strings_for_schema`）

顶层 `coerce_tool_args` 只修第一层。但开源模型（DeepSeek、Qwen、GLM）可能把**嵌套元素**也 JSON-encode：

```json
{"todos": ["{\"id\": \"1\", \"content\": \"x\"}"]}
//         ↑ 整个元素是字符串，不是 dict
```

这个函数按 schema 递归进入 `items` 和 `properties`，逐层解析 JSON 字符串。

---

## 七、Hook 系统集成

`handle_function_call` 是 hook 系统的三大触点：

| Hook | 时机 | 作用 |
|---|---|---|
| `tool_request` middleware | handler 执行前 | 插件改写 args |
| `pre_tool_call` | handler 执行前 | 安全审查、白名单拦截（background_review 的 memory/skill 白名单在这里生效！） |
| `post_tool_call` | handler 执行后 | 观察者通知（日志、遥测） |
| `transform_tool_result` | post_tool_call 之后 | 插件改写返回给 LLM 的结果 |

**`has_hook()` 门控**：post_tool_call 和 transform_tool_result 在没注册 listener 时完全跳过——一次 dict lookup 的开销。

---

## 八、与你已读模块的连接点

| 你读过的模块 | 在 model_tools 的触点 |
|---|---|
| **Background Review** | `handle_function_call` Step 4 → `resolve_pre_tool_block` → `plugins.py._get_pre_tool_call_directive_details` → 白名单检查 |
| **Curator** | curator fork 创建 AIAgent 时不传 toolsets → `get_tool_definitions` 加载全量工具 → curator 的 prompt 约束 LLM 只用部分 |
| **Kanban** | `get_tool_definitions` Step 1 检测 `HERMES_KANBAN_TASK` 环境变量 → 强制注入 kanban toolset |
| **Prompt Builder** | 两个模块独立但配合：prompt_builder 产出 system prompt 文本，model_tools 产出 `tools[]` 数组，一起发给 LLM |
| **Tirith** | 不直接交互。tirith 是终端命令执行前的扫描，在 `terminal` handler 内部调用，不在本层 |

---

## 九、`_compute_tool_definitions` 逐段深读

以下是对 7 步流程中关键代码段的逐一解析，来自逐行讨论。

### 9.1 缓存失效的四层策略（第 303-310 行注释）

```python
# Fast path: memoized result when the caller doesn't need stdout prints.
# The cache key captures every argument-level input; the registry
# generation captures registry mutations (MCP refresh, plugin load).
# check_fn results are TTL-cached one level down, inside
# registry.get_definitions. The config-mtime fingerprint below captures
# user-visible config edits that affect dynamic schemas ...
```

四层失效策略，各管各的变化来源：

| 变化来源 | 怎么感知 | 粒度 |
|---|---|---|
| toolset 参数不同 | 缓存键里直接比较 `frozenset(enabled_toolsets)` 等 | 精确 |
| 新工具注册/注销（MCP刷新、插件加载） | `registry._generation` 自增 | 精确 |
| check_fn 结果变了（Docker 启停、API key 配置） | registry 内部 30s TTL，不在本层 | 30秒延迟 |
| 用户改了 config.yaml（execute_code模式、discord allowlist） | `(mtime_ns, size)` 指纹 | 精确 |

关键洞察：缓存键涵盖了两类「参数级」变化（参数不同、环境变量不同）和两类「外部世界」变化（registry 突变、配置变更），check_fn 的 TTL 留给 registry 自己管，本层不操心。

### 9.2 Step 1：Kanban Worker 强制注入（第 369-375 行）

```python
if os.environ.get("HERMES_KANBAN_TASK") and "kanban" not in effective_enabled_toolsets:
    effective_enabled_toolsets.append("kanban")
```

场景：`coder` profile 只配了 `coding` toolset 省 token。但 Dispatcher 用它派 worker 时，worker 必须能调 `kanban_complete` / `kanban_block` / `kanban_heartbeat`，否则跑完任务无法汇报，看板上任务变僵尸。

为什么用 `effective_enabled_toolsets` 而非直接改 `enabled_toolsets`？拷贝一份再追加，不污染外部传入的参数。

为什么不会影响非 kanban agent？`HERMES_KANBAN_TASK` 是 Dispatcher 通过 `subprocess.Popen` 注入子进程的环境变量，进程级隔离。用户手动 `hermes chat`、Gateway 消息、curator/background_review fork 都没有这个变量。

### 9.3 Step 2：disabled_toolsets 的减法语义（第 427 行）

```python
tools_to_include.difference_update(resolved)  # 等价于 set -= other
```

流程：`Step 1: enabled 往里加 → Step 2: disabled 往外减`。disabled 优先级高于 enabled——同时出现在两边就被踢掉。

唯一例外：platform bundle（`hermes-*`）和 posture toolset 被 disable 时只删非核心工具，保 `_HERMES_CORE_TOOLS` 不被误伤（否则禁一个 bundle 会把 bash/read_file 都干掉）。

### 9.4 Step 4：三类动态 schema 修正

**核心原因**：工具的 schema 描述里交叉引用了其他工具。如果那些工具在当前 session 不存在，模型会被误导去调用不存在的工具。

#### execute_code（第 457-464 行）—— 重建整个 schema

```python
sandbox_enabled = SANDBOX_ALLOWED_TOOLS & available_tool_names
dynamic_schema = build_execute_code_schema(sandbox_enabled, mode=_get_execution_mode())
```

`execute_code` 是「工具里的工具」——启动沙箱后，模型在沙箱内还能调一组受限工具（`web_search`、`read_file` 等 7 个）。如果某工具在当前 session 不可用，必须从沙箱描述里删掉。

解法：取 `SANDBOX_ALLOWED_TOOLS` 与 `available_tool_names` 的交集 → 重建内层 schema。

#### discord/discord_admin（第 466-493 行）—— 重建整个 schema

```python
dynamic = schema_fn()  # 内部调 GET /applications/@me 查 bot 实际 intents
if dynamic is None:
    filtered_tools = [t for t in filtered_tools if ... != discord_tool_name]
```

Discord bot 的 privileged intents 决定了它能做什么操作。静态 schema 列出所有可能功能，但实际能用的取决于 Discord API 实时查询结果。

解法：动态生成 schema。如果返回 `None`（bot 未连接或没有 intent）→ 直接把工具从列表里删掉，模型不知道有 discord。

#### browser_navigate（第 495-513 行）—— 只做一次字符串替换

```python
desc = desc.replace(
    " For simple information retrieval, prefer web_search or web_extract (faster, cheaper).",
    "",
)
```

静态描述里有一句「建议先用 web_search，更快更便宜」。如果 `web` toolset 被禁了，这句话会误导模型。

解法：能一行 `str.replace` 解决的就不重建 schema。

### 9.5 Step 5：schema_sanitizer（第 525-535 行）

```python
filtered_tools = sanitize_tool_schemas(filtered_tools)
```

跨后端兼容层。llama.cpp 用 schema 生成 GBNF 文法来约束 tool call 输出，对格式要求远严于云服务商。问题包括：

- `"type": "object"` 但没写 `properties` → GBNF parser 不知道该生成什么
- MCP server 返回的字段值是裸字符串而非合法 schema 节点

对正常 schema 是 no-op，只修有问题的。外层 try-except 兜底——sanitizer 挂了打 warning 继续走，不拖死工具加载。

### 9.6 Step 6：Tool Search 渐进式披露（第 537-565 行）

```python
if not skip_tool_search_assembly and ts_cfg.enabled != "off":
    assembly = assemble_tool_defs(filtered_tools, context_length=..., config=ts_cfg)
    filtered_tools = assembly.tool_defs
```

必须放最后一步——前面 5 步已经把 schema 修成最终版本，token 数可以精确估算。

判断逻辑：
1. 工具分两类：核心工具（`_HERMES_CORE_TOOLS`）永远不 defer；MCP + 插件工具是候选项
2. 估算可 defer 工具的 token 占用
3. 超过阈值（默认上下文窗口的 10%）→ 替换为 `tool_search` + `tool_describe` + `tool_call` 三个桥接工具
4. 没超过阈值 → 不替换，原样返回

`skip_tool_search_assembly=True` 的作用：让桥接 handler 自己能看到**完整工具目录**而非被替换后的 3 个桥接工具。否则 `tool_search` 只能搜到自己 → 桥接彻底失效。

### 9.7 降级策略

贯穿整个 Step 4-6 的设计哲学：

| 步骤 | 挂了怎么办 |
|---|---|
| execute_code 动态重建 | 不在 `available_tool_names` 里就不触发，静默跳过 |
| discord 动态重建 | 返回 None → 删工具，不影响其他工具 |
| browser 字符串替换 | 没匹配到 → 不替换，保持原样 |
| schema_sanitizer | exception → warning，跳过 sanitize |
| Tool Search assembly | exception → warning，用完整工具列表（工具多总比没工具强） |

**永远不因为一个工具的优化失败而阻断整个工具加载流程。**

---

## 十、`handle_function_call` 逐段深读

### 10.1 方法签名（15 个参数）

参数分四组：

**核心参数（必传）：**
| 参数 | 含义 |
|---|---|
| `function_name` | LLM 要调的工具名，如 `"bash"`、`"read_file"` |
| `function_args` | LLM 传的参数 dict，如 `{"command": "ls"}` |

**追踪参数（全链路透传）：** `task_id`、`tool_call_id`、`session_id`、`turn_id`、`api_request_id`——不参与调度逻辑，但透传给所有 hook、middleware、approval 模块，用于关联 tool call 到具体会话和轮次。

**跳过标志（防止重复执行）：**

| 参数 | 含义 |
|---|---|
| `skip_pre_tool_call_hook` | `run_agent.py` 传 True——它自己已调过一次 hook |
| `skip_tool_request_middleware` | 跳过 args 改写中间件（bridge 递归时用） |

**Tool Search 作用域（安全隔离）：** `enabled_toolsets` / `disabled_toolsets` 限定 bridge 搜到的工具范围；`enabled_tools` 传递给 `execute_code` handler 限定沙箱内工具。

### 10.2 Step 0 — 类型修正（第 1066-1069 行）

```python
function_args = coerce_tool_args(function_name, function_args)
```

第一道防线。放在最前面是因为后续所有步骤（middleware、hook、handler）都期望拿到类型正确的参数。如果在 middleware 之后再修，middleware 可能已经因为 `"10"` 不是 `10` 做了错误判断。

### 10.3 Step 1 — Tool Search 桥接分发（第 1071-1144 行）

三种桥接工具的分发：

```
is_bridge_tool(function_name)?
  ├─ tool_search → dispatch_tool_search(args, current_defs)   ← 纯读，直接返回
  ├─ tool_describe → dispatch_tool_describe(args, current_defs) ← 纯读，直接返回
  └─ tool_call → resolve → 递归 handle_function_call(真实tool名)
```

`current_defs` 获取时传 `skip_tool_search_assembly=True`——bridge handler 必须看到完整工具列表而非已被替换的自己。同时传 `enabled_toolsets` / `disabled_toolsets` 做作用域裁剪：受限 session 通过 bridge 也只能搜到自己的工具。

`tool_call` 递归时做了**双重防御**：
1. `resolve_underlying_call()` 检查工具在全局 deferrable 注册表中
2. 额外检查是否在当前 session 的 deferrable 目录中——防止越权

递归调用的精妙之处：bridge 对下游完全透明。递归进入后，pre_tool_call hook、edit approval、post_tool_call hook 看到的全是真实工具名 `jira_create_ticket`，不知道 `tool_call` 的存在。

### 10.4 Step 2 — Tool Request Middleware（第 1146-1164 行）

```python
_tool_original_args = dict(function_args)
_tool_request_mw = apply_tool_request_middleware(...)
function_args = _tool_request_mw.payload            # 改写后的
_tool_original_args = _tool_request_mw.original_payload  # 真正原始值
```

插件可在 handler 执行前改写参数。`_tool_original_args` 保留改写前的值，供后续审批对比（「用户想执行什么 vs 实际执行什么」）。bridge 递归时 `skip_tool_request_middleware=True`——已从 `tool_call` 解包出真实参数，不需要 middleware 再改写。

### 10.5 Step 3 — Agent Loop 工具短路（第 1167-1168 行）

```python
if function_name in _AGENT_LOOP_TOOLS:  # {"todo", "memory", "session_search", "delegate_task"}
    return json.dumps({"error": f"{function_name} must be handled by the agent loop"})
```

这 4 个工具需要读写 AIAgent 实例内部状态，`handle_function_call` 是纯函数没有 agent 引用。在此主动短路——正常情况 `run_agent.py` 会在调用前拦截，不会走到这里。

### 10.6 Step 4 — Pre-Tool-Call Hook / 审批（第 1170-1214 行）

```python
if not skip_pre_tool_call_hook:
    block_message = resolve_pre_tool_block(function_name, function_args, ...)
    if block_message is not None:
        result = json.dumps({"error": block_message})
        _emit_post_tool_call_hook(..., status="blocked", ...)  # 被拦也要通知观察者
        return result
```

`resolve_pre_tool_block` 内部**同时处理 block（直接拦截）和 approve（人工审批）两条路径**：

```
resolve_pre_tool_block()
  ├─ invoke_hook("pre_tool_call", ...)  ← 调所有插件
  └─ 按 directive 处理：
       ├─ action="block"   → 返回 block_message
       ├─ action="approve" → request_tool_approval() → 弹窗/推送等用户
       │   ├─ 同意 → 返回 None（放行）
       │   ├─ 拒绝 → 返回 block_message
       │   └─ 超时 → 返回 block_message（fail-closed）
       └─ action="allow"   → 返回 None（放行）
```

对 `handle_function_call` 来说，block 和 approve-denied 没区别——`block_message != None → return error JSON`。被拦截后仍 emit `post_tool_call` hook（status="blocked"），遥测/日志插件需要知道拦截事件。

Single-fire contract（单次触发协议）：

```
run_agent.py._invoke_tool:
  1. resolve_pre_tool_block()  ← 第一次 hook
  2. handle_function_call(skip_pre_tool_call_hook=True)
     → 跳过 Step 4，不二次触发
```

### 10.7 Step 5 — ACP/Zed Edit Approval（第 1216-1228 行）

```python
# The requester is bound via ContextVar only for ACP sessions, so CLI/gateway
# paths are unaffected when it is unset.
edit_block_message = maybe_require_edit_approval(function_name, function_args)
```

独立于 Step 4。ACP（Agent Communication Protocol）是 Zed 编辑器集成协议——AI 在编辑器里改代码需要经过编辑审批。审批上下文通过 Python 的 `ContextVar` 绑定：ACP session 初始化时 `set`，CLI/Gateway 从来没 `set` 过 → `maybe_require_edit_approval` 发现没绑定 → 直接返回 `None`（放行），**零开销**。

guard 本身抛异常的兜底：

```python
except Exception:
    if function_name in {"write_file", "patch"}:
        return json.dumps({"error": "Edit approval denied: approval guard failed"})
```

只对写操作 fail-closed——读文件 guard 挂了不管，写文件 guard 挂了必须拦。宁可误拦也不能让未经审批的写入溜过去。

### 10.8 Step 6 — Read-Loop 计数器重置（第 1230-1237 行）

```python
if function_name not in _READ_SEARCH_TOOLS:  # {"read_file", "search_files"}
    notify_other_tool_call(task_id or "default")
```

`file_tools` 内部有一套**连续重复读取检测**，每当执行非读操作时通过此调用重置计数器。

**检测机制（在 `file_tools.py` 中）：**

```python
# 每次 read_file 被调用时
read_key = ("read", path, offset, limit)  # 精确匹配：同一文件 + 同一范围

if task_data["last_key"] == read_key:
    task_data["consecutive"] += 1   # 和上一次读的完全一样 → +1
else:
    task_data["consecutive"] = 1    # 换了文件或范围 → 重置
```

触发条件很严格——必须连续用**完全相同的 `(path, offset, limit)`** 重复读取才算，模型分两次读同一文件的不同段落不会误触发。

**三级响应：**

| 次数 | 行为 | 模型看到什么 |
|---|---|---|
| 1-2 | 正常 | 文件内容 |
| 3 | 🟡 警告 | 文件内容 + `_warning: "You have read this exact file region 3 times..."` |
| ≥4 | 🔴 硬拦截 | `{"error": "BLOCKED: You have read this exact file region N times in a row..."}` — 不返回文件内容 |

模型被硬拦截后的逃逸路径：执行一次非读工具（`bash`、`write_file` 等） → 触发 `notify_other_tool_call` → 计数器归零 → 下次 `read_file` 又是 count=1。设计假定：如果模型中间干了别的活，说明在推进任务，重新读文件大概率有新的意图而不是死循环。

### 10.9 Step 7 — 真正执行（第 1239-1298 行）

三层包装：

**1. Observability Context（第 1246-1258 行）：**

```python
_approval_tokens = set_current_observability_context(
    turn_id=turn_id or "",
    tool_call_id=tool_call_id or "",
)
```

把 `turn_id` 和 `tool_call_id` 绑定到当前线程的 `ContextVar` 上。用途：如果这个 tool call 触发了审批弹窗，审批系统在处理用户回复时需要知道「这笔审批属于哪个 tool call」——它从 ContextVar 里读 `turn_id`/`tool_call_id` 来精准匹配。用 ContextVar 而非逐层传参是因为审批弹窗可能发生在调用链深处（`dispatch` → handler → `check_dangerous_command` → `_run_approval_gate`），改所有中间函数签名不现实。`finally` 块中 `reset` 恢复，防止当前 tool call 的上下文泄漏到下一个。

**2. execute_code 特殊处理：**

```python
sandbox_enabled = enabled_tools if enabled_tools is not None else _last_resolved_tool_names
```

优先用调用方显式传入的 `enabled_tools`——`_last_resolved_tool_names` 是进程级全局，subagent 可能覆写它。用显式参数防止 session 间污染。

**3. `_dispatch` 闭包的两个变体：**

```python
# execute_code 路径
def _dispatch(next_args):
    return registry.dispatch(function_name, next_args,
                             task_id=task_id, session_id=session_id,
                             enabled_tools=sandbox_enabled)

# 非 execute_code 路径
def _dispatch(next_args):
    return registry.dispatch(function_name, next_args,
                             task_id=task_id, session_id=session_id,
                             user_task=user_task)
```

区别：`execute_code` 额外传 `enabled_tools`（沙箱内可用工具列表）；非 `execute_code` 额外传 `user_task`（用户原始任务描述，供 `browser_snapshot` 等工具判断上下文）。

为什么包成闭包而不是直接调？`_dispatch` 传给 `run_tool_execution_middleware` 作为回调——middleware 在外层注入超时控制、重试逻辑。两个变体的参数差异在闭包里「固化」了，middleware 看到的是统一签名的 `(args) → result`。

**4. Execution Middleware 包裹 + 计时：**

```python
_dispatch_start = time.monotonic()   # 不受系统时间调整影响
result = run_tool_execution_middleware(
    function_name, function_args, _dispatch,
    original_args=_tool_original_args, ...
)
duration_ms = int((time.monotonic() - _dispatch_start) * 1000)
```

`duration_ms` 传给后续的 `post_tool_call` 和 `transform_tool_result` hook，插件可做延迟监控。

### 10.10 Step 8 & 9 — Post-Tool-Call 与 Transform Tool Result 的区别

两个 hook 在 handler 执行后依次触发，但角色完全不同：

| | `post_tool_call` | `transform_tool_result` |
|---|---|---|
| 能改返回值吗 | ❌ 不能 | ✅ 能 |
| 返回值被用吗 | 不关心 | 第一个有效字符串替换原 result |
| 目的 | 记录、遥测、日志、告警 | 清洗、脱敏、格式化 |
| 执行顺序 | 先执行 | 后执行 |
| 需要解析 result | 不解析，原样传 | 解析 error 状态 |
| 门控 | `has_hook("post_tool_call")` | `has_hook("transform_tool_result")` |

执行流程：

```
registry.dispatch() 返回 result
  │
  ├─ post_tool_call        ← 先：观察者记录（「bash 被调了，耗时 1.2s」）
  │                         拿到的是原始 result
  │
  └─ transform_tool_result ← 后：改写者修整（「result 里有 API key，替换成 ***」）
       │
       ▼
  return result ← 修整后的 result 返回给 LLM
```

为什么需要两个分开：安全插件用 `post_tool_call` 记录每一次 tool call（包括被 block 的），不需要改返回值。脱敏插件用 `transform_tool_result` 在返回值到达 LLM 之前抹掉敏感信息——如果挂在 `post_tool_call` 上，改了的 result 不会被传回去。

`transform_tool_result` 的 fail-open 设计：

```python
for hook_result in hook_results:
    if isinstance(hook_result, str):
        result = hook_result
        break  # 第一个有效字符串生效，其余忽略
```

- 非字符串返回值 → 忽略
- 插件抛异常 → 原 result 不受影响
- 无有效返回 → 原 result 不变

脱敏插件挂了最多泄漏敏感信息（坏），但不会让工具调用失败（更坏）。故意的取舍。

### 10.12 兜底异常处理

整个 Step 3-9 包在 `try/except` 里：

```python
except Exception as e:
    return json.dumps({"error": _sanitize_tool_error(error_msg)})
```

任何步骤炸了都不会让对话循环崩溃——异常变成 `{"error": "..."}` JSON 返回给 LLM，LLM 自行决定重试或换方案。`_sanitize_tool_error` 清洗异常消息中的 XML 标签和代码块标记。

### 10.13 设计原则

| 原则 | 体现 |
|---|---|
| 防御性深度 | 类型修正 → middleware → hook → approval → dispatch，层层过滤 |
| Bridge 透明 | `tool_call` 递归后 hook/middleware 看到的是真实工具名 |
| Single-fire | `skip_pre_tool_call_hook` 防止重复触发 hook |
| Fail-closed 安全操作 | 写文件 guard 挂了 → 阻止 |
| Fail-open 增值功能 | middleware/hook 挂了 → 跳过，不阻断 |
| 观察与决策分离 | post_tool_call 只管通知，transform_tool_result 管改写 |
| 兜底不崩溃 | 整个方法 try/except，异常变 error JSON |

---

## 十一、审批链路完整追踪

从 plugin 说「需要审批」到用户点按钮、再到 agent 线程被唤醒，完整路径：

### 11.1 跨文件调用链

```
model_tools.py: resolve_pre_tool_block()
  → plugins.py: invoke_hook("pre_tool_call")
    → plugin 返回 {"action": "approve", "message": "需要确认"}
  → plugins.py: request_tool_approval(tool_name, message, rule_key=...)
    → approval.py: _run_approval_gate(...)
      → _await_gateway_decision(...)   ← 🔴 Agent 线程阻塞在这里
```

### 11.2 plugin 审批 vs tirith 审批

两者共用同一扇 `_run_approval_gate`，区别只在触发者：

| | tirith 审批 | plugin 审批 |
|---|---|---|
| 触发条件 | bash 命令匹配到危险 pattern | plugin 返回 `action="approve"` |
| 适用范围 | 只对 `terminal` | 任何工具 |
| `pattern_key` | `"cmd_pattern:{hash}"` | `"plugin_rule:{tool_name}:{reason_hash}"` |
| `display_target` | 实际命令文本 | `"<tool_name> (plugin approval rule)"` |

`pattern_key` 命名空间隔离，plugin 的 `[a]lways` 不会误放 tirith 危险命令。

### 11.3 `_await_gateway_decision` — 同步阻塞等待

```python
# approval.py:2588-2609
while True:
    if is_interrupted():           # 用户 /stop？
        entry.result = "deny"      # 自己填拒绝
        entry.event.set()          # 自己唤醒自己
        break
    if _deadline - now <= 0:       # 超时？
        break
    if entry.event.wait(timeout=min(1.0, _remaining)):  # ← 被唤醒！
        resolved = True
        break
    touch_activity_if_due(...)     # 每 ~10s 发心跳，防看门狗杀 session

# 退出循环后
choice = entry.result   # ← 直接读，Gateway 线程已填好
```

每秒醒来一次而不是一直 block，是为了检查中断信号、超时、以及发送 activity heartbeat。

### 11.4 线程间通信：`entry.result` + `threading.Event`

**写入方（Gateway 的 async 线程）：**

```python
# approval.py:1535 — 由 Gateway 的 /approve 或 /deny handler 调用
def resolve_gateway_approval(session_key, choice, reason=None):
    for entry in targets:
        entry.result = choice       # ← 先写结果
        entry.reason = reason
        entry.event.set()           # ← 再唤醒等待线程
```

先写 `entry.result`，再 `set()`——标准的多线程通信模式，保证唤醒后 `entry.result` 已有值。

**等待方（Agent 的执行线程）：**

```python
if entry.event.wait(...):  # True = 被 set() 唤醒
    resolved = True
    choice = entry.result   # "once" / "session" / "always" / "deny"
```

三种退出方式：

| 退出方式 | `resolved` | `choice` | 含义 |
|---|---|---|---|
| `event.set()` 被唤醒 | True | `"once"/"always"/"deny"` | 用户做了决定 |
| 超时 | False | None | 没人回复 |
| `is_interrupted()` | True | `"deny"` | 用户发了 /stop |

---

## 十二、`model_tools.py` vs `tool_executor.py`

两个文件都在工具执行链路中，但分属不同层级。

### 调用链路

```
run_agent.py 的对话循环（while 循环）
  │
  ▼
tool_executor.execute_tool_calls_sequential() 或 _concurrent()
  │
  ├─ 中断检查（每个 tool call 之前）
  ├─ 参数解析（JSON parse）
  ├─ nudge 计数器重置（memory / skill_manage）
  ├─ Tool Search bridge 解包（agent 层 scope gate）
  ├─ tool‑request middleware
  ├─ guardrail 检查
  ├─ 中断注册（并发模式下注册 worker tid 到 agent）
  │
  ├─ agent._invoke_tool()  ← run_agent.py 的方法（转发）
  │   └─ agent_runtime_helpers.invoke_tool()
  │       ├─ coerce_tool_args
  │       ├─ pre_tool_call hook（第一次触发）
  │       └─ model_tools.handle_function_call(skip_pre_tool_call_hook=True)
  │           ├─ Step 1: Bridge 分发
  │           ├─ Step 2: middleware
  │           ├─ Step 3-5: 安全检查
  │           ├─ Step 6: read-loop 重置
  │           ├─ Step 7: registry.dispatch()
  │           ├─ Step 8: post_tool_call
  │           └─ Step 9: transform_tool_result
  │
  ├─ 结果写入 session DB
  ├─ 结果显示（emoji、label、spinner、preview）
  ├─ budget 检查（按 context window 截断大结果）
  ├─ post‑tool‑call 回调
  └─ messages.append(tool_result_message)  ← 构造成 OpenAI 格式
```

### 分工对比

| 关注点 | `model_tools.py` | `tool_executor.py` |
|---|---|---|
| **粒度** | 单个 tool call | 一个 assistant message 里的全部 tool call |
| **状态** | 无状态纯函数 | 接收 agent 实例，读写 agent 内部状态 |
| **调度策略** | 不关心 | 并发（ThreadPoolExecutor 最多 8 线程）vs 顺序 |
| **中断** | 不处理 | 每个 tool call 前检查 `_interrupt_requested` |
| **显示** | 不显示 | spinner、emoji、tool label、参数预览 |
| **Bridge 解包** | Step 1，在递归调用中处理 | 自己做一次 unwrap（agent 层的 scope gate） |
| **Guardrail** | 不处理 | 工具执行前的安全检查 |
| **Nudge 计数** | 不管理 | `memory`/`skill_manage` 调用后重置计数器 |
| **Session DB** | 不写 | 结果写入 session DB（`_flush_session_db_after_tool_progress`） |
| **Budget** | 不限制 | `maybe_persist_tool_result` 按 context window 动态截断 |
| **结果构造** | 返回原始 JSON 字符串 | 构造成 OpenAI 格式的 tool_result message 追加到 messages |
| **参数解析** | 不解析原始 JSON | `_parse_tool_arguments` 先解析 LLM 的 raw JSON 参数 |
| **谁调用** | curator、background_review、agent 等 | 只有 agent 的对话循环 |

### 一句话总结

`tool_executor.py` 是 **agent 层**的工具编排器——管理一个 turn 内所有 tool call 的**完整生命周期**（中断、显示、guardrail、持久化、budget）；`model_tools.py` 是**调度层**的纯函数——只做「名→函数→结果」的**单次映射**。前者调后者，前者懂 agent，后者只懂工具。

---

## 十三、快速参考：10 秒定位

| 想找什么 | 看哪 |
|---|---|
| LLM 能调哪些工具是怎么决定的 | `get_tool_definitions` → `_compute_tool_definitions` 的 7 步流程 |
| 某个 tool call 为什么被拦截了 | `handle_function_call` Step 4 的 `resolve_pre_tool_block` |
| 用户审批是怎么挂起和唤醒的 | 第十一节：`_await_gateway_decision` + `resolve_gateway_approval` 线程通信 |
| read-loop 怎么检测和阻断 | 10.8 + `file_tools.py` 的三级响应 |
| post_tool_call 和 transform_tool_result 区别 | 10.10：观察者 vs 改写者 |
| 工具参数类型不对怎么修的 | `coerce_tool_args` + `_normalize_json_strings_for_schema` |
| 一个工具调用的完整生命周期 | `handle_function_call` 的 Step 0→9 |
| agent 层编排 vs 调度层纯函数 | 第十二节：`tool_executor.py` vs `model_tools.py` |
| 为什么工具 handler 能混用 sync/async | `_run_async` 的三级策略 |
| Tool Search bridge 怎么做到透明的 | `handle_function_call` Step 1 的递归调用 + `skip_tool_search_assembly` |

---

文件本身不长（1381 行），但连接了几乎所有子系统。你可以把它当作 Hermes 工具系统的「配电箱」——进线是 LLM 的 `tool_use`，出线是 50+ 个 handler，中间经过 middleware、hook、白名单、审批、类型修正五道保险。
