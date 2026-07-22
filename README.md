# 学习 Hermes Agent

讨论记录和知识整理，方便复习和后续对话上下文。

## 目录

| 文档 | 内容 |
|---|---|
| [kanban-全面解析.md](kanban-全面解析.md) | 看板系统：架构分层、9工具详解、注册机制、与Agent关系 |
| [curator-后台技能维护编排器.md](curator-后台技能维护编排器.md) | Curator：技能自动维护、umbrella-building、归档分类融合 |
| [background_review-后台审查机制.md](background_review-后台审查机制.md) | 后台记忆/技能审查：触发机制、fork创建、白名单拦截、缓存 |
| [model_tools-工具调度枢纽.md](model_tools-工具调度枢纽.md) | 工具调度：get_tool_definitions、handle_function_call、类型修正 |
| [hermes_state-SQLite会话状态存储.md](hermes_state-SQLite会话状态存储.md) | SQLite 会话存储：95个方法、FTS5搜索、WAL并发 |
| [prompt_builder-系统提示词拼装工厂.md](prompt_builder-系统提示词拼装工厂.md) | Prompt 拼装：system prompt、技能索引、上下文文件加载 |
| [tirith_security.md](tirith_security.md) | Tirith：命令安全扫描、退出码裁决、熔断器 |
| [AIAgent参数记录.md](AIAgent参数记录.md) | AIAgent 50个参数分类速查 |

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
