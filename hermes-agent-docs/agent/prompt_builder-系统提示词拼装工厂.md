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

## 五、技能系统：索引 vs 完整内容

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

## 六、上下文文件优先级链

`build_context_files_prompt()` 按以下优先级加载项目上下文，**第一个匹配到的生效**（互斥，不合并）：

```
1. .hermes.md / HERMES.md    ← 从 cwd 向上走到 git root 查找
2. AGENTS.md / agents.md     ← 仅 cwd
3. CLAUDE.md / claude.md     ← 仅 cwd
4. .cursorrules + .cursor/rules/*.mdc  ← 仅 cwd
```

SOUL.md（`~/.hermes/SOUL.md`）是独立的，不受此优先级影响，总是单独加载。

---

## 七、阅读建议

如果第一次读这个文件：

1. **先看 3 个 `build_*` 函数** — 这是全部对外接口，理解它们的返回值就知道文件做什么
2. **再看各类 GUIDANCE 常量的开头注释** — 每段都有详细注释解释为什么需要这段引导、针对哪些模型
3. **`PLATFORM_HINTS` 扫一眼结构即可** — 20+ 个平台，用到哪个看哪个
4. **`build_skills_system_prompt()` 是读懂缓存策略的入口** — 内存 LRU → 磁盘快照 → 文件扫描 → 外部目录
5. **`KANBAN_GUIDANCE` 可略过** — 除非关注多 Agent 协作
