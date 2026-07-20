# 学习 Hermes Agent

讨论记录和知识整理，方便复习和后续对话上下文。

## 目录

| 文档 | 内容 |
|---|---|
| [kanban-architecture.md](kanban-architecture.md) | 看板系统全景：架构分层、核心文件、代码位置 |
| [kanban-9-tools.md](kanban-9-tools.md) | 9 个 Kanban 工具详解：Worker 自管理 / 协作 / Orchestrator |
| [kanban-tool-registration.md](kanban-tool-registration.md) | 工具注册机制：registry → discover → toolset 三步链条 |
| [kanban-and-agent.md](kanban-and-agent.md) | 看板与 AI Agent 的关系：Dispatcher → Worker(spawn) → AI Agent |

## 项目概述

Hermes Agent 是 Nous Research 开发的个人 AI Agent 框架。核心概念：

```
第 ① 层：核心 — AIAgent 对话循环（run_agent.py）
第 ② 层：入口 — CLI / Gateway / TUI / Desktop（都调用同一核心）
第 ③ 层：编排 — Kanban（多 Agent 看板）/ Delegate（委托）/ Cron（定时）
```

## 关键入口文件

| 优先级 | 文件 | 职责 |
|---|---|---|
| ⭐⭐⭐ | `run_agent.py` | 核心对话循环（while 循环 + API 调用 + 工具执行） |
| ⭐⭐ | `model_tools.py` | 工具发现、注册、调用的枢纽 |
| ⭐ | `agent/prompt_builder.py` | 系统提示词构建 |
| ⭐⭐⭐ | `cli.py` | CLI 交互入口 |
| ⭐⭐ | `gateway/run.py` | 消息平台 Gateway 入口 |
