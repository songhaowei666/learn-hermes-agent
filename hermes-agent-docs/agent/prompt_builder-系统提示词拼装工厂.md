# prompt_builder.py — 系统提示词拼装工厂

> 文件位置：`agent/prompt_builder.py`（约 1995 行）
> 分析日期：2026-07-20

---

## 一、模块定位

`prompt_builder.py` 是 Hermes Agent 的**系统提示词（system prompt）组装模块**。它本身不运行，只提供一组无状态函数，由 `AIAgent._build_system_prompt()` 按需调用，把人格、平台、环境、技能索引、项目上下文等片段拼装成一份完整的 system prompt 发送给 LLM。

一句话概括：**Hermes 的「大脑设定工厂」——决定 Agent 知道自己是谁、在哪跑、有什么工具、该守什么规矩。**

---

## 二、文件结构

```
├── 常量区（1-465 行）
│   ├── DEFAULT_AGENT_IDENTITY           — Agent 默认人设
│   ├── TOOL_USE_ENFORCEMENT_GUIDANCE    — 强制工具调用
│   ├── TASK_COMPLETION_GUIDANCE         — 防止早停/伪造输出
│   ├── PARALLEL_TOOL_CALL_GUIDANCE      — 批量工具调用引导
│   ├── OPENAI_MODEL_EXECUTION_GUIDANCE  — GPT/Codex/Grok 执行纪律
│   ├── GOOGLE_MODEL_OPERATIONAL_GUIDANCE— Gemini/Gemma 操作规范
│   ├── KANBAN_GUIDANCE                  — 多 Agent 协作协议
│   ├── MEMORY_GUIDANCE                  — 记忆系统使用指导
│   └── SKILLS_GUIDANCE                  — 技能系统使用指导
│
├── 平台提示词（624-866 行）
│   └── PLATFORM_HINTS[20+ 平台]         — 每平台格式规则 + MEDIA: 语法
│
├── 环境感知（874-1168 行）
│   ├── build_environment_hints()        — 主入口，返回环境描述文本
│   ├── _probe_remote_backend()          — 对 Docker/SSH 等发探针取真实环境
│   └── WSL_ENVIRONMENT_HINT             — WSL 路径翻译提示
│
├── 中间转向标记（595-615 行）
│   ├── STEER_MARKER_OPEN/CLOSE          — 用户中途插话的标记位
│   └── format_steer_marker()            — 包装函数
│
├── 技能索引（1250-1706 行）
│   ├── build_skills_system_prompt()     — 主入口，扫描技能目录生成索引
│   ├── 两层缓存：内存 LRU + 磁盘快照    — 避免每次扫描文件系统
│   └── 外部技能目录支持                  — 只读 dir，local 优先
│
├── 上下文文件加载（1776-1994 行）
│   ├── build_context_files_prompt()     — 主入口，优先级链发现项目上下文
│   ├── load_soul_md()                   — ~/.hermes/SOUL.md 作为人格定义
│   └── 威胁扫描 _scan_context_content() — 防止 prompt injection
│
└── 其他
    ├── computer_use_guidance()          — 桌面自动化场景指导（平台自适应）
    └── build_nous_subscription_prompt() — Nous 订阅能力状态
```

---

## 三、对外接口（6 个函数）

| 函数 | 返回值 | 调用场景 |
|---|---|---|
| `load_soul_md()` | `str or None` | 每次构建 system prompt，作为 identity slot #1 |
| `build_environment_hints()` | `str` | 每次构建，告诉模型 OS/工作目录/是否容器 |
| `build_skills_system_prompt()` | `str` | 每次构建，生成 `<available_skills>` 索引块 |
| `build_context_files_prompt()` | `str` | 每次构建，注入 `.hermes.md`/`AGENTS.md` 等项目文档 |
| `build_nous_subscription_prompt()` | `str` | 条件调用，有 Nous 订阅时才生成 |
| `computer_use_guidance()` | `str` | 有 `computer_use` 工具集时注入 |

辅助接口：
- `format_steer_marker()` — 包装用户中途 /steer 指令
- `drain_truncation_warnings()` — 取走上下文截断警告供外层展示
- `clear_skills_system_prompt_cache()` — 技能增删改后清缓存

---

## 四、核心设计决策

| # | 决策 | 落地点 |
|---|------|--------|
| 1 | **无状态函数库** | 所有函数 stateless，`AIAgent._build_system_prompt()` 是唯一调方 |
| 2 | **模型感知引导注入** | 看模型名（`TOOL_USE_ENFORCEMENT_MODELS`）决定是否注入特定指导 |
| 3 | **平台自适应格式** | `PLATFORM_HINTS` 按 Telegram/Discord/CLI/WebUI 等给出不同 markdown/MEDIA 规则 |
| 4 | **远程后端透明化** | 非 local 后端（docker/ssh/modal）发送探针取真实环境，抑制宿主信息 |
| 5 | **技能索引两层缓存** | 内存 LRU（8 槽）+ 磁盘快照（mtime/size 校验），避免每次扫描 |
| 6 | **上下文安全扫描** | `_scan_context_content` 拦截 prompt injection，阻断后返回 `[BLOCKED]` 占位符 |
| 7 | **动态截断上限** | 上下文文件大小按模型窗口 `0.06` 比例动态缩放，floor 20K / ceiling 500K |

---

## 五、两层缓存机制（深入）

`build_skills_system_prompt()` 使用两层缓存，**粒度不同**：

```
build_skills_system_prompt() 被调用
        │
        ▼
┌──────────────────────────────────────────────┐
│ Layer 1: 内存 LRU (OrderedDict, 最多 8 槽)    │
│ 键: (skills_dir, external_dirs, tools,       │
│      toolsets, platform_hint, disabled,      │
│      compact_categories)                     │
│ 值: 渲染好的完整 prompt 字符串                │
│                                              │
│ ✅ 命中 → move_to_end() 晋升 → 直接返回       │
│ ❌ 未命中 → 进入 Layer 2                       │
└──────────────────────────────────────────────┘
        │ (miss)
        ▼
┌──────────────────────────────────────────────┐
│ Layer 2: 磁盘快照                              │
│ 文件: ~/.hermes/.skills_prompt_snapshot.json  │
│ 内容: { version, manifest, skills[],          │
│         category_descriptions }               │
│                                              │
│ manifest = 每个 SKILL.md/DESCRIPTION.md 的    │
│            [st_mtime_ns, st_size] → 变了就失效 │
│                                              │
│ ✅ manifest 匹配 → 用预解析 metadata 组装      │
│ ❌ 不匹配/不存在 → 冷路径（全量文件扫描）       │
└──────────────────────────────────────────────┘
        │ (miss)
        ▼
   遍历所有 SKILL.md → parse_frontmatter()
   → 写磁盘快照 → 写 LRU → 返回
```

### 两层存的内容不同

| 层 | 存的内容 | 粒度 |
|---|---|---|
| Layer 1 (LRU) | 渲染完成的 prompt 文本 | 按调用参数分槽 |
| Layer 2 (快照) | 预解析的 metadata（skill_name, category, description, platforms, conditions） | 只管文件是否变化 |

### LRU 晋升机制

```python
_SKILLS_PROMPT_CACHE.move_to_end(cache_key)   # 命中时推到末尾（最新）
_SKILLS_PROMPT_CACHE.popitem(last=False)      # 淘汰时弹出开头（最老）
```

`move_to_end` 是 OrderedDict 的方法，作用是把 key 移到最末尾作为"最新使用"。没有这一步，缓存就退化为 FIFO（先进先出），意味着常用条目也会被优先淘汰。加上后实现真正的 LRU 语义：最近用过的不会被 `popitem(last=False)` 弹出。

### cache_key 的组成

```python
cache_key = (
    str(skills_dir),                              # ~/.hermes/skills/
    tuple(str(d) for d in external_dirs),          # config.yaml 配置的外部目录
    tuple(sorted(str(t) for t in available_tools)),
    tuple(sorted(str(ts) for ts in available_toolsets)),
    _platform_hint,                                # "telegram" / "cli" / "" 等
    tuple(sorted(disabled)),                       # 当前平台禁用的技能名
    tuple(sorted(compact_categories or ())),       # 编码模式下折叠的类别
)
```

`_platform_hint` 来自环境变量 `HERMES_PLATFORM` 或 `HERMES_SESSION_PLATFORM`，约 22 个值：`whatsapp`, `telegram`, `discord`, `slack`, `cli`, `tui`, `cron`, `webui`, `desktop`, `wecom`, `weixin`, `qqbot`, `yuanbao` 等。gateway 服务多个平台时，不同平台的 disabled 技能列表不同，必须按平台分槽。

### 快照的目的：冷启动优化，并非 Prompt Cache

快照是为了**进程重启后**不用重新 `os.walk` + `parse_frontmatter` 所有 SKILL.md，省的是文件 IO + YAML 解析的 CPU 时间。LLM 层面的 prompt caching（如 Anthropic prefix caching）是另一层，发生在服务端。

### 外部技能目录：无快照

外部目录（`config.yaml` → `skills.external_dirs`）**没有** Layer 2 磁盘快照。注释明确说：
> Scan external dirs directly (no snapshot caching — they're read-only and typically small)

但外部目录的结果**会**写入 Layer 1 LRU，所以进程内第二次调用仍可命中。

```
              ┌────────────┬────────────┬──────────┐
              │ Layer 1 LRU │ Layer 2 快照 │ 冷扫描   │
├─────────────┼────────────┼────────────┼──────────┤
│ 本地 skills  │     ✅      │     ✅      │ 两层miss  │
│ 外部 skills  │     ✅      │     ❌      │ 每次必走  │
└─────────────┴────────────┴────────────┴──────────┘
```

### frontmatter_name vs skill_name

`_build_snapshot_entry` 返回的 entry 包含两个名字字段：

| 字段 | 来源 | 示例 |
|---|---|---|
| `skill_name` | SKILL.md 的**父目录名** | `skills/code-review/SKILL.md` → `"code-review"` |
| `frontmatter_name` | YAML frontmatter 中的 `name:` 字段，未设置时 fallback 到 `skill_name` | `name: Code Review` → `"Code Review"` |

外部目录去重和 disabled 判断都使用 `frontmatter_name`，因为技能索引里展示给模型看的就是这个名字。

---
## 七、技能系统：索引 vs 完整内容

这个概念容易混淆，单独说明：

```
┌─────────────────────────────────────────────────────┐
│ System Prompt（每轮必在）                              │
│  ┌───────────────────────────────────────────────┐  │
│  │ <available_skills>                             │  │
│  │   code-review: Review code for bugs...         │  │  ← 只有名字 + 一句话描述
│  │   deep-research: Multi-source research...      │  │     （由 build_skills_system_prompt 生成）
│  │   ...                                          │  │
│  │ </available_skills>                            │  │
│  └───────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────┐
│ Conversation History（窗口内有效）                      │
│  ┌───────────────────────────────────────────────┐  │
│  │ user: "审查代码"                                │  │
│  │ assistant: → skill_view("code-review")         │  │
│  │ tool result: { "content": "完整的 200 行        │  │  ← 完整内容（作为 tool result 存在）
│  │                SKILL.md 正文..." }              │  │     窗口满了就丢，不会自动重新注入
│  └───────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
```

关键结论：
- **技能索引**每轮都在 system prompt 里，告诉模型「有哪些技能可用」
- **完整技能内容**是 `skill_view` 的 tool result，存在于会话历史中
- **不会自动重新注入**：如果历史被压缩/截断，模型必须再次调用 `skill_view`
- `/skill-name` 斜杠命令加载的技能**以 user message 形式注入**，同样只在窗口内有效

---

## 八、上下文文件优先级链

`build_context_files_prompt()` 按以下优先级加载项目上下文，**第一个匹配到的生效**（互斥，不合并）：

```
1. .hermes.md / HERMES.md    ← 从 cwd 向上走到 git root 查找
2. AGENTS.md / agents.md     ← 仅 cwd
3. CLAUDE.md / claude.md     ← 仅 cwd
4. .cursorrules + .cursor/rules/*.mdc  ← 仅 cwd
```

SOUL.md（`~/.hermes/SOUL.md`）是独立的，不受此优先级影响，总是单独加载。

---

## 九、阅读建议

如果第一次读这个文件：

1. **先看 3 个 `build_*` 函数** — 这是全部对外接口，理解它们的返回值就知道文件做什么
2. **再看各类 GUIDANCE 常量的开头注释** — 每段都有详细注释解释为什么需要这段引导、针对哪些模型
3. **`PLATFORM_HINTS` 扫一眼结构即可** — 20+ 个平台，用到哪个看哪个
4. **`build_skills_system_prompt()` 是读懂缓存策略的入口** — 内存 LRU → 磁盘快照 → 文件扫描 → 外部目录
5. **`KANBAN_GUIDANCE` 可略过** — 除非关注多 Agent 协作
