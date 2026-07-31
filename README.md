# Hermes Agent - 命令解析流程文档

本文档详细记录了 Hermes Agent CLI 的命令解析流程，包括：
1. 命令行入口到解析器（parser）的完整链路
2. `--toolsets` 参数从 CLI 参数到最终工具过滤的完整流转

## 文档目录

| 文档 | 内容 |
|------|------|
| [01-命令入口到解析器.md](01-命令入口到解析器.md) | `hermes chat` 如何找到 `_parser.py`，argparse 的构建与 dispatch 机制 |
| [02-toolsets参数流转.md](02-toolsets参数流转.md) | `--toolsets "web,terminal"` 从命令行到模型 API 的 6 步完整流转 |

## 关键文件索引

| 文件 | 作用 |
|------|------|
| `pyproject.toml` | 声明 `hermes` 命令入口点 → `hermes_cli.main:main` |
| `hermes_cli/main.py` | `main()` 入口函数，组装 parser 并 dispatch |
| `hermes_cli/_parser.py` | 顶层 parser + `chat` 子 parser 构建 |
| `cli.py` | `HermesCLI` 类 + `cli.main()` 函数（fire.Fire 入口） |
| `run_agent.py` | `AIAgent` 类 + `run_agent.main()` 函数（直接调用入口） |
| `agent/agent_init.py` | `init_agent()` - 初始化 agent，存储 toolsets 并调用工具过滤 |
| `model_tools.py` | `get_tool_definitions()` / `_compute_tool_definitions()` - 核心过滤引擎 |
| `toolsets.py` | `resolve_toolset()` / `validate_toolset()` - 工具集定义与解析 |
| `hermes_cli/cli_agent_setup_mixin.py` | `_init_agent()` - CLI 模式下创建 AIAgent 实例 |

## 两条入口路径

```
                    ┌──────────────┐
                    │  hermes CLI  │
                    └──────┬───────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
         hermes        hermes       hermes
         (无参数)      chat -q      --tui
              │            │            │
              ▼            ▼            ▼
         cmd_chat()   cmd_chat()   _launch_tui()
              │            │            │
              └────────────┼────────────┘
                           │
                           ▼
                     HermesCLI()          ← cli.py
                           │
                           ▼
                 _init_agent()            ← cli_agent_setup_mixin.py
                           │
                           ▼
                      AIAgent()           ← run_agent.py
                           │
                           ▼
                    init_agent()          ← agent/agent_init.py
                           │
                           ▼
               get_tool_definitions()     ← model_tools.py
```

## 版本信息

- 项目：hermes-agent
- 分析日期：2026-07-31
