# 看板系统全面解析

> 基于 Hermes Agent 源码讨论整理，涵盖架构全景、代码位置、工具注册机制、9个工具详解、与 AI Agent 的关系。

---

## 目录

1. [一句话总结](#一句话总结)
2. [架构分层](#架构分层)
3. [核心文件速查](#核心文件速查)
4. [任务状态流](#任务状态流)
5. [与 AI Agent 的关系](#与-ai-agent-的关系)
6. [Dispatcher 调度流程](#dispatcher-调度流程)
7. [9 个 Kanban 工具详解](#9-个-kanban-工具详解)
8. [工具注册机制](#工具注册机制)
9. [权限设计](#权限设计)

---

## 一句话总结

**Kanban 是把多个 AI Agent（`hermes chat` 实例）按任务面板编排成流水线的调度框架。** 底层复用 `hermes chat` 的 AI Agent 能力，上层叠加了任务状态机、SQLite 持久化、CLAIM 锁并发控制、依赖链路、通知投递等编排维度。

---

## 架构分层

```
┌──────────────────────────────────────────────┐
│  CLI (hermes kanban ...)  │  Dashboard UI    │  ← 人机交互层
├──────────────────────────────────────────────┤
│  tools/kanban_tools.py                       │  ← Agent 工具层（模型可调用）
├──────────────────────────────────────────────┤
│  hermes_cli/kanban_db.py    (引擎核心)       │  ← 数据库 + 调度 + 生命周期
│  hermes_cli/kanban.py       (CLI命令)        │
│  hermes_cli/kanban_specify.py  (triage规格化) │
│  hermes_cli/kanban_decompose.py (triage分解)  │
│  hermes_cli/kanban_swarm.py    (swarm拓扑)    │
│  hermes_cli/kanban_diagnostics.py (诊断引擎)  │
├──────────────────────────────────────────────┤
│  gateway/kanban_watchers.py                  │  ← Gateway 调度器 + 通知器
├──────────────────────────────────────────────┤
│  plugins/kanban/dashboard/plugin_api.py      │  ← Dashboard 后端 API
│  plugins/kanban/dashboard/dist/              │  ← Dashboard 前端
└──────────────────────────────────────────────┘
```

---

## 核心文件速查

| 文件 | 行数 | 职责 |
|---|---|---|
| `hermes_cli/kanban_db.py` | ~8600 | **引擎核心** — SQLite 表结构、任务 CRUD、调度器 dispatch_once、worker spawn、通知订阅、附件管理 |
| `hermes_cli/kanban.py` | ~2800 | **CLI 接口** — 所有 `hermes kanban <verb>` 命令 |
| `hermes_cli/kanban_specify.py` | 274 | **Triage 规格化** — 调 LLM 把模糊 triage 任务转为具体 spec |
| `hermes_cli/kanban_decompose.py` | 478 | **Triage 分解** — 调 LLM 把任务拆成子任务图 |
| `hermes_cli/kanban_swarm.py` | 279 | **Swarm 拓扑** — 并行专家 worker + 验证器 + 合成器的 DAG |
| `hermes_cli/kanban_diagnostics.py` | 1134 | **诊断引擎** — 检测幻觉卡片、重复失败、阻塞循环等异常 |
| `tools/kanban_tools.py` | ~1680 | **Agent 工具层** — 9 个模型可调用的 kanban 工具 |
| `gateway/kanban_watchers.py` | ~1280 | **Gateway 层** — 调度器循环 + 事件通知投递 |
| `plugins/kanban/dashboard/plugin_api.py` | ~2500 | **Dashboard API** — FastAPI REST + WebSocket |

---

## 任务状态流

```
triage → todo → ready → running → done
                    ↑         ↓
                    │      blocked ← 人处理 → unblock → ready
                    │         ↓
                    └─── archived
```

| 状态 | 含义 |
|---|---|
| **triage** | 模糊需求，需要 specifier profile 来规格化 |
| **todo** | 已明确但还不满足启动条件（如等待父任务完成） |
| **ready** | 等待调度器分配 worker |
| **running** | worker 正在执行 |
| **blocked** | 卡住了：dependency / needs_input / capability / transient |
| **done** | 完成，带结构化 handoff |
| **archived** | 归档（软删除） |

---

## 与 AI Agent 的关系

### 核心证据

`hermes_cli/kanban_db.py` 第 7904 行 `_default_spawn()`：

```python
def _default_spawn(task, workspace, *, board=None):
    """Fire-and-forget ``hermes -p <profile> chat -q ...`` subprocess."""

    profile_arg = normalize_profile_name(task.assignee)
    prompt = f"work kanban task {task.id}"

    cmd = [
        *_resolve_hermes_argv(),
        "-p", profile_arg,           # 谁来做（AI 角色 profile）
        "--cli",
    ]
    if task.skills:                  # 注入特定技能
        for sk in task.skills:
            cmd.extend(["--skills", sk])
    if task.model_override:          # 可覆盖模型
        cmd.extend(["-m", task.model_override])
    cmd.extend(["chat", "-q", prompt])  # 启动 AI 对话

    env["HERMES_KANBAN_TASK"] = task.id
    env["HERMES_KANBAN_BOARD"] = resolved_board
    env["HERMES_PROFILE"] = profile_arg

    proc = subprocess.Popen(cmd, env=env, ...)
```

### 对比

| | 用户手动 | Kanban Worker |
|---|---|---|
| 启动方式 | `hermes -p techlead chat "帮我写代码"` | Dispatcher `subprocess.Popen` 同样命令 |
| Profile | 用户选的 | task.assignee |
| Skills | 用户加载的 | task.skills + kanban 自动注入 |
| 对话循环 | `run_agent.py` 的 `run_conversation()` | 完全相同 |
| 可用工具 | 正常工具集 | 正常工具集 + kanban 工具（`HERMES_KANBAN_TASK` 环境变量触发） |

### 叠层关系

```
                     ┌──────────────────────────┐
                     │    Kanban 调度编排层      │
                     │  · 任务状态机            │
                     │  · SQLite 持久化         │
                     │  · CLAIM 锁并发控制      │
                     │  · 依赖链路              │
                     │  · 通知投递              │
                     ├──────────────────────────┤
                     │    AI Agent 层（复用）    │
                     │  · hermes chat 对话循环  │
                     │  · 工具调用              │
                     │  · Profile 角色定义      │
                     │  · Skills 技能系统       │
                     └──────────────────────────┘
```

**Kanban 没有重新实现 Agent —— 它只是在 Agent 之上加了任务管理和编排。**

### 多 Profile 协作

```
看板上的任务：
┌────────────────────────────────────────────┐
│  "调研竞品AI框架"  → assignee: researcher   │
│  "实现登录模块"    → assignee: coder         │
│  "Code Review PR"  → assignee: reviewer      │
│  "写周报"          → assignee: writer        │
└────────────────────────────────────────────┘
         │                    │
         ▼                    ▼
  hermes -p researcher   hermes -p coder
  chat -q "work kanban   chat -q "work kanban
  task abc123"           task def456"
```

所有 worker 共享同一个看板（SQLite DB），通过 `kanban_comment` 传递信息，通过 `kanban_link` 建立依赖，通过 `kanban_complete` 的 `summary/metadata` 做结构化交接。

---

## Dispatcher 调度流程

Gateway 进程后台循环（默认每 60 秒）：

```
  release_stale_claims()
      → 回收超时的 claim（worker 死了但没清理）
  
  recompute_ready()
      → 检查依赖：父任务都 done 的 todo 任务 → ready
  
  dispatch_once()
      → 遍历 ready 任务
      → CAS 原子抢任务（SQLite WAL + BEGIN IMMEDIATE）
      → _default_spawn() 启动 worker 子进程
      → 记录 run_id、PID
  
  detect_crashed_workers()
      → 检查已启动 worker 的 PID 是否还活着
      → 死了 → 回收 claim → 重试或 block
```

---

## 9 个 Kanban 工具详解

定义在 `tools/kanban_tools.py` 行 1602-1681，分为三类权限。

### 一、Worker 自管理（所有 kanban worker 都能用）

#### 1. `kanban_show` — 读取任务上下文 📋

**什么时候用**：开工前、retry 时重新定向

**返回**：任务 title/body/assignee、父任务 handoff、之前的尝试记录、评论和事件日志、预格式化的 `worker_context`

#### 2. `kanban_complete` — 标记完成 ✔

**参数**：

| 参数 | 说明 |
|---|---|
| `summary` | 1-3 句话描述做了什么（给人看的） |
| `metadata` | 结构化数据：changed_files, tests_run, findings... |
| `created_cards` | 你创建的卡片 ID 列表（kernel 验证真实性，防止幻觉） |
| `artifacts` | 产出物绝对路径：PDF、图表、报告...（Gateway 作为附件发送给人） |

**安全机制**：`created_cards` 中不存在的 ID 会阻止完成操作。

#### 3. `kanban_block` — 暂停任务 ⏸

**block kind**：

| kind | 含义 | 恢复方式 |
|---|---|---|
| `dependency` | 等另一个任务完成 | 自动 — 依赖完成后自动 unblock |
| `needs_input` | 需要人决策/回答 | 人工 — 人回复后手动 unblock |
| `capability` | 硬限制：没权限、缺凭证 | 人工 — 需要配置/授权 |
| `transient` | 临时故障，重试可能解决 | 人工/自动 |

**诊断保护**：同一任务反复 block/unblock 会被诊断引擎报警（根因没解决）。

#### 4. `kanban_heartbeat` — 存活信号 💓

**什么时候用**：长时间操作（训练、爬虫、编码），每隔几分钟发一次。可选 `note` 描述进度。

---

### 二、协作者交互

#### 5. `kanban_comment` — 追加评论 💬

**作用**：写跨 worker 的持久笔记 — 部分发现、设计决策、踩过的坑。不要用于临时推理。

#### 6. `kanban_create` — 创建新任务 ➕

**关键参数**：

| 参数 | 说明 |
|---|---|
| `title` | 必填 |
| `assignee` | 必填，哪个 profile 执行 |
| `body` | 详细规格、验收标准——下游 worker 会读到 |
| `parents` | 父任务 ID 列表——全 done 后才 ready |
| `triage` | true → 先入 triage 列 |
| `idempotency_key` | 幂等键，防重试创建重复卡片 |
| `skills` | 注入特定技能 |
| `goal_mode` | true → worker 跑 goal loop（多轮自我检查） |
| `workspace_kind` | scratch / dir / worktree |

**典型 fan-out**：Orchestrator 拆大任务 → 并发 researcher → 汇总 writer（`parents` 依赖两个 researcher）。

#### 7. `kanban_link` — 建立依赖边 🔗

**参数**：`parent_id` + `child_id`。拒绝环依赖和自引用。

---

### 三、Orchestrator 专属

这两个工具 `check_fn=_check_kanban_orchestrator_mode`，只有启用了 `kanban` toolset 且非 dispatcher worker 的 profile 才能看到。

#### 8. `kanban_list` — 浏览全板 📋

按 assignee / status / tenant 过滤，总览板上所有任务。

#### 9. `kanban_unblock` — 解除阻塞 ▶

把 blocked 任务放回 ready 队列。

---

## 工具注册机制

### 从注册到暴露的完整链条

```
tools/kanban_tools.py          tools/registry.py            toolsets.py
─────────────────────          ─────────────────            ───────────
registry.register(...)  ──→   自动扫描 tools/*.py     ──→   TOOLSETS["kanban"]
(9次，模块级别)                AST解析 + importlib           显式列出9个工具名
                                                                     │
                                                                     ▼
                                        check_fn 运行时判断 ──→  只有 kanban worker 或
                                                               启用了 kanban toolset 的
                                                               profile 才能看到这些工具
```

### 第一步：声明注册

`tools/kanban_tools.py` 末尾，模块级别直接调用：

```python
registry.register(
    name="kanban_show",          # 工具名（对应 LLM function call name）
    toolset="kanban",            # 所属 toolset
    schema=KANBAN_SHOW_SCHEMA,   # OpenAI 格式的 function schema
    handler=_handle_show,        # Python 处理函数
    check_fn=_check_kanban_mode, # 运行时可用性判断
    emoji="📋",                  # UI 展示用
)
```

**`import tools.kanban_tools` 时自动执行注册，无需手动维护任何导入列表。**

### 第二步：自动发现

`tools/registry.py` 的 `discover_builtin_tools()`：

1. 扫描 `tools/` 目录下所有 `.py` 文件
2. 文本预过滤：文件必须同时包含 `"registry"` 和 `"register"`（快速排除不相关文件）
3. AST 解析：找到 `registry.register(...)` 调用
4. `importlib.import_module()` 导入 → 触发注册

**开发者只需**把 `.py` 放在 `tools/` 目录并写 `registry.register()`，其他全自动。

### 第三步：Toolset 接线

`toolsets.py` 显式列出工具名：

```python
"kanban": {
    "description": "Kanban multi-agent coordination ...",
    "tools": [
        "kanban_show", "kanban_list", "kanban_complete", "kanban_block",
        "kanban_heartbeat", "kanban_comment",
        "kanban_create", "kanban_link",
        "kanban_unblock",
    ],
},
```

**注册 ≠ 暴露**。必须出现在 toolset 中，且该 toolset 被启用，才能发给 LLM。

### 第四步：运行时门控

```python
def _check_kanban_mode() -> bool:
    if os.environ.get("HERMES_KANBAN_TASK"):
        return True                              # dispatcher 派出的 worker
    return _profile_has_kanban_toolset()         # 配置了 kanban toolset 的 profile
```

### 设计原则

三道控制，互不耦合：

| 层级 | 控制什么 | 在哪定义 |
|---|---|---|
| `registry.register()` | 工具"存在" | 工具文件自己 |
| `TOOLSETS["kanban"].tools` | 工具"可被暴露" | `toolsets.py` |
| `check_fn` | 工具"此时可用" | 工具文件自己 |

体现了 AGENTS.md 强调的：**核心要窄，能力在边缘**。

---

## 权限设计

### 两种检查函数

- `_check_kanban_mode` — 6 个通用工具 + kanban_link：dispatcher worker 或 orchestrator profile 都能用
- `_check_kanban_orchestrator_mode` — `kanban_list` 和 `kanban_unblock`：只有 orchestrator 能用

### 权限矩阵

```
                          Worker          Orchestrator
                          ──────          ─────────────
kanban_show               ✅              ✅
kanban_complete           ✅              ✅
kanban_block              ✅              ✅
kanban_heartbeat          ✅              ✅
kanban_comment            ✅              ✅
kanban_create             ✅              ✅
kanban_link               ✅              ✅
kanban_list               ❌              ✅
kanban_unblock            ❌              ✅
```

**设计意图**：干活的 agent 只管理自己的任务，管全局的 orchestrator 才有权限看全貌和干预流程。
