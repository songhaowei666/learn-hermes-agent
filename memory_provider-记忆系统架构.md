# MemoryProvider / MemoryManager —— Hermes 可插拔记忆系统

## 概述

Hermes 的记忆系统采用**策略模式 + 门面模式**，由两层构成：

- **`MemoryProvider`**（`agent/memory_provider.py`）：抽象基类（ABC），定义所有记忆后端必须遵循的契约
- **`MemoryManager`**（`agent/memory_manager.py`）：编排器，持有多个 Provider 实例，统一调度并添加横切关注点

---

## 一、MemoryProvider —— 定义"能做什么"

### 核心设计约束

- **单一外部提供者**：`MemoryManager` 确保同时只运行一个外部记忆后端（内置除外），避免工具 schema 膨胀和后端冲突
- **插件化注册**：插件放在 `plugins/memory/<name>/` 下，通过 `memory.provider` 配置键激活

### 必须实现的抽象方法

| 方法 | 作用 |
|------|------|
| `name` | 提供者标识（如 `'honcho'`、`'hindsight'`） |
| `is_available()` | 检查配置、凭证、依赖是否就绪（不做网络调用） |
| `initialize(session_id)` | 会话启动时的初始化——创建资源、建立连接 |
| `get_tool_schemas()` | 返回暴露给模型的工具 schema（OpenAI function calling 格式） |

### 核心生命周期方法（可选覆写）

| 方法 | 调用时机 | 说明 |
|------|---------|------|
| `system_prompt_block()` | 组装系统提示词时 | 提供静态说明文本 |
| `prefetch(query)` | **每次 API 调用前** | 召回相关上下文（同步读缓存） |
| `queue_prefetch(query)` | **每次 turn 后** | 后台预热下一轮上下文（异步计算） |
| `sync_turn(user, asst)` | **每次 turn 后** | 异步持久化对话回合 |
| `handle_tool_call(name, args)` | 模型调用该提供者的工具时 | 分发处理 |
| `shutdown()` | 清理退出 | 刷新队列、关闭连接 |

### 可选钩子（opt-in）

| 钩子 | 触发场景 |
|------|---------|
| `on_turn_start()` | turn 开始时——计数、作用域管理 |
| `on_session_end()` | 会话结束时——事实提取、摘要 |
| `on_session_switch()` | `/resume`、`/branch`、`/reset`、上下文压缩等 |
| `on_pre_compress()` | 上下文压缩丢弃旧消息前——抢救关键信息 |
| `on_delegation(task, result)` | 子 Agent 完成时——父 Agent 侧观察委托结果 |
| `on_memory_write()` | 内置记忆工具写入时——镜像同步到外部后端 |
| `backup_paths()` | 声明 `HERMES_HOME` 之外的磁盘路径，确保备份不丢失 |

---

## 二、`queue_prefetch` 的两阶段预取机制

### 核心问题

记忆后端的召回操作很重（向量搜索 + LLM 推理 + 网络请求），如果在 `prefetch()` 里同步执行，每轮对话都要等几百毫秒到几秒。

### 解决方案

**`queue_prefetch` 提前算，`prefetch` 直接取：**

```
第 N 轮结束
  ├─ sync_all(用户消息, 助手回复)       ← 把本轮对话写入记忆后端
  ├─ queue_prefetch_all(用户消息)       ← 【后台线程】为 N+1 轮预热
  │
第 N+1 轮开始
  ├─ prefetch_all(用户消息)             ← 从缓存取回已算好的结果（几乎零延迟）
  ├─ 注入系统提示词 → 模型调用
```

调用位置：[run_agent.py:3416-3424](../hermes-agent-main/run_agent.py#L3416-L3424)

```python
self._memory_manager.sync_all(user_text, response_text, ...)   # 持久化
self._memory_manager.queue_prefetch_all(user_text, ...)         # 预热下一轮
```

### 为什么用上一轮消息预热下一轮？

N+1 轮的 `prefetch` 消费的是基于第 N 轮消息预热的缓存，"落后一轮"。这仍然可以接受的原因：

1. **对话连续性**：第 N+1 轮往往是第 N 轮的追问或延伸，推理结果大概率相关
2. **模型可自行忽略**：内容以 `<memory-context>` 标签包裹，模型被告知是参考数据，不相关会自动跳过
3. **首轮同步兜底**：首轮无缓存时（如 Honcho），会用带超时的同步调用直接对当前消息做推理
4. **本质是延迟换速度的权衡**：用落后一轮的精准度换零感知延迟

### 两层记忆架构（以 Honcho 为例）

**Layer 1 — Base Context（用户画像层）**：跨会话累积的用户表示（偏好、习惯、知识背景），跟具体查询弱相关，用 N 轮或 N+1 轮消息触发差异不大。

**Layer 2 — Dialectic Supplement（推理补充层）**：LLM 对用户消息做深度推理，查询相关性强。这一层是落后一轮的。

### 各提供者策略差异

| 策略 | 提供者 | 延迟 | 精准度 |
|------|--------|------|--------|
| 异步预热 + 同步取缓存（落后一轮） | Honcho、Hindsight、RetainDB | 低 | 中 |
| 完全同步（每轮实时查） | OpenViking、ByteRover、Supermemory、Holographic | 高 | 高 |
| 首轮同步 + 后续用缓存 | Mem0 | 中 | 中 |

---

## 三、MemoryManager —— 编排"何时做、怎么做"

### 角色定位

`MemoryManager` 是 `run_agent.py` 中**唯一的记忆系统集成点**。`run_agent.py` 不知道背后挂的是 Honcho 还是 Hindsight，只与 `MemoryManager` 交互。

### 调用链

```python
self._memory_manager = MemoryManager()
self._memory_manager.add_provider(builtin_provider)   # 内置（始终先注册）
self._memory_manager.add_provider(honcho_provider)     # 外部（最多一个）

# 每轮生命周期：
self._memory_manager.build_system_prompt()              # → fan-out system_prompt_block()
self._memory_manager.prefetch_all(user_msg)             # → fan-out prefetch()
# ... LLM 调用 ...
self._memory_manager.handle_tool_call(name, args)       # → 路由到对应提供者
self._memory_manager.sync_all(user, asst)               # → 后台线程 fan-out sync_turn()
self._memory_manager.queue_prefetch_all(user_msg)       # → 后台线程 fan-out queue_prefetch()
```

### Manager 层独有的横切能力

| 增强项 | 说明 |
|--------|------|
| **后台线程调度** | `sync_all` / `queue_prefetch_all` 提交到单线程 `DaemonThreadPoolExecutor`，防止慢后端阻塞主流程 |
| **skill 脚手架剥离** | `_strip_skill_scaffolding()` 统一从消息中剥离 `/skill` 包装，避免污染记忆存储 |
| **错误隔离** | 每个 provider 调用都套 `try/except`，单个失败不影响其他 |
| **单外部提供者限制** | `add_provider()` 拒绝注册第二个外部提供者 |
| **核心工具名保护** | 外部提供者工具名不得与 `clarify`、`delegate_task` 等内置核心工具冲突 |
| **工具路由** | 根据工具名找到对应提供者并分发 `handle_tool_call` |
| **上下文围栏** | `build_memory_context_block()` 将记忆用 `<memory-context>` XML 标签包裹 |
| **流式输出擦除** | `StreamingContextScrubber` 处理跨 chunk 标签，防止泄露到 UI |
| **优雅关闭** | 先排空后台队列（5秒超时），再逆序关闭所有提供者 |
| **内置记忆镜像** | `notify_memory_tool_write()` 将内置 `memory` 工具的写入同步到外部提供者 |

### 与 MemoryProvider 的关系图

```
MemoryProvider (ABC)              MemoryManager (编排器)
定义契约                           持有 Provider 列表 + 调度逻辑
                                  
prefetch()         ◄──fan-out──  prefetch_all()
queue_prefetch()   ◄──fan-out──  queue_prefetch_all()
sync_turn()        ◄──fan-out──  sync_all()
system_prompt_..() ◄──fan-out──  build_system_prompt()
handle_tool_call() ◄──路由────  handle_tool_call()
on_session_end()   ◄──fan-out──  on_session_end()
shutdown()         ◄──fan-out──  shutdown_all()
                                  + 工具路由表
                                  + 后台线程池
                                  + 上下文擦除/围栏
                                  + skill 脚手架剥离
```

---

## 四、关键文件索引

| 文件 | 职责 |
|------|------|
| `agent/memory_provider.py` | MemoryProvider 抽象基类 |
| `agent/memory_manager.py` | MemoryManager 编排器 |
| `run_agent.py:3367-3426` | `_sync_external_memory_for_turn` —— 每轮结束触发 sync + queue_prefetch |
| `agent/turn_context.py:558-565` | 每轮开始调用 `prefetch_all` |
| `agent/conversation_loop.py:797-812` | 将记忆上下文注入 LLM 调用 |
| `plugins/memory/honcho/` | Honcho 提供者（最复杂的实现，两层记忆 + 节律控制） |
| `plugins/memory/hindsight/` | Hindsight 提供者 |
| `plugins/memory/openviking/` | OpenViking 提供者（同步策略，queue_prefetch 为空操作） |
| `plugins/memory/mem0/` | Mem0 提供者 |
| `plugins/memory/retaindb/` | RetainDB 提供者 |
