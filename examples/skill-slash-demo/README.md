# Skill Slash Command Demo

用 **LangChain + LangGraph** 复刻 Hermes Agent 的 `/技能名` 斜杠命令机制。

## 架构

```
skills/
├── gif-search/SKILL.md      ← 技能定义（YAML 前置元数据 + Markdown 主体）
├── dogfood/SKILL.md
└── code-review/SKILL.md

demo.py                       ← 主入口
  ├── scan_skills()            扫描 → {"/slug": skill_info}
  ├── build_skill_message()    构建 LLM 消息
  └── LangGraph 流水线:
        parse_input → load_skill → build_message → call_llm → 输出
```

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 设置 OpenAI API Key
export OPENAI_API_KEY="sk-..."
# 可选：自定义模型或端点
export SKILL_DEMO_MODEL="gpt-4o-mini"
export OPENAI_BASE_URL="https://your-proxy/v1"

# 3. 列出可用技能
python demo.py --list

# 4. 调用技能
python demo.py "/gif-search funny cats"
python demo.py "/dogfood https://example.com login flow"
python demo.py "/code-review def foo(): pass"

# 5. 调试模式 —— 只看构建的 prompt，不调 LLM
python demo.py --dry-run "/gif-search cats"
```

## 和 Hermes Agent 的对应关系

| Hermes Agent | 本 Demo |
|---|---|
| `agent/skill_commands.py::scan_skill_commands()` | `demo.py::scan_skills()` |
| `agent/skill_commands.py::build_skill_invocation_message()` | `demo.py::build_skill_message()` |
| `agent/skill_commands.py::_build_skill_message()` | `demo.py::build_skill_message()` |
| `cli.py::process_command()` 调度逻辑 | `demo.py::cmd_invoke()` → LangGraph 流水线 |
| `_pending_input.put(msg)` 注入 LLM | LangGraph `call_llm` 节点 |
| `extract_user_instruction_from_skill_message()` | （精简版省略，按需扩展） |

## LangGraph 流水线

```
START
  │
  ▼
parse_input     "/gif-search funny cats" → slug + instruction
  │
  ▼
load_skill      扫描 skills/ 目录，匹配 slug → 加载 SKILL.md
  │
  ▼
build_message   组装: [IMPORTANT...] + skill body + user instruction
  │
  ▼
call_llm        LangChain ChatOpenAI 调用，返回结果
  │
  ▼
END
```

每个节点是一个纯函数，状态通过 `SkillState` TypedDict 传递。

## 添加新技能

在 `skills/` 下创建新目录和 `SKILL.md`：

```markdown
---
name: my-skill
description: "My custom skill description."
---

# My Skill

## Instructions
1. Do X
2. Do Y
```

重启即可：`python demo.py "/my-skill some input"`
