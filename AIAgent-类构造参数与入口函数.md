# AIAgent 类 — 构造参数与业务入口函数

> 源码位置：[run_agent.py:393-565](run_agent.py#L393-L565)

## 概述

`AIAgent` 是 Hermes Agent 的门面类（Facade），管理对话流程、工具执行和模型响应。该类本身**不包含业务逻辑**——构造和所有方法都是转发器，真正逻辑在 `agent/` 子模块中：

- **构造转发** → `agent.agent_init.init_agent()`
- **对话转发** → `agent.conversation_loop.run_conversation()`

---

## 构造参数（共 70+ 个）

### 1. 模型与 API 配置

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `base_url` | `str` | `None` | API 基础 URL（有 property setter，同步更新 `_base_url_lower` 和 `_base_url_hostname`） |
| `api_key` | `str` | `None` | API 密钥 |
| `provider` | `str` | `None` | 提供商标识 |
| `api_mode` | `str` | `None` | API 模式 |
| `model` | `str` | `""` | 模型名称 |
| `max_tokens` | `int` | `None` | 最大输出 token 数 |
| `reasoning_config` | `Dict[str, Any]` | `None` | 推理模型配置（如 extended thinking） |
| `service_tier` | `str` | `None` | 服务等级 |
| `request_overrides` | `Dict[str, Any]` | `None` | 请求级别覆盖参数 |
| `fallback_model` | `Dict[str, Any]` | `None` | 回退模型配置（主模型不可用时） |

### 2. Agent 子进程管理

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `command` | `str` | `None` | 子进程命令 |
| `args` | `list[str]` | `None` | 子进程参数 |
| `acp_command` | `str` | `None` | ACP（Agent Communication Protocol）命令 |
| `acp_args` | `list[str]` | `None` | ACP 参数 |

### 3. 行为控制

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `max_iterations` | `int` | 90 | 最大工具调用迭代次数（主 agent 与子 agent 共享） |
| `tool_delay` | `float` | 1.0 | 工具调用间延迟（秒） |
| `save_trajectories` | `bool` | `False` | 是否保存完整轨迹 |
| `verbose_logging` | `bool` | `False` | 详细日志模式 |
| `quiet_mode` | `bool` | `False` | 静默模式 |
| `tool_progress_mode` | `str` | `"all"` | 工具进度显示模式 |
| `log_prefix_chars` | `int` | 100 | 日志前缀字符数 |
| `log_prefix` | `str` | `""` | 日志前缀字符串 |
| `iteration_budget` | `IterationBudget` | `None` | 迭代预算实例 |

### 4. 工具集控制

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled_toolsets` | `List[str]` | `None` | 启用的工具集白名单 |
| `disabled_toolsets` | `List[str]` | `None` | 禁用的工具集黑名单 |

### 5. 提供商标识过滤

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `providers_allowed` | `List[str]` | `None` | 允许的提供商标识白名单 |
| `providers_ignored` | `List[str]` | `None` | 忽略的提供商标识黑名单 |
| `providers_order` | `List[str]` | `None` | 提供商标识优先级顺序 |
| `provider_sort` | `str` | `None` | 提供商标识排序方式 |
| `provider_require_parameters` | `bool` | `False` | 必须带有提供商标识参数 |
| `provider_data_collection` | `str` | `None` | 提供商标识数据收集策略 |
| `openrouter_min_coding_score` | `Optional[float]` | `None` | OpenRouter 最低编程评分阈值 |

### 6. 会话与上下文

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `session_id` | `str` | `None` | 当前会话 ID |
| `parent_session_id` | `str` | `None` | 父会话 ID（子 agent 场景） |
| `pass_session_id` | `bool` | `False` | 是否将会话 ID 传递给子 agent |
| `gateway_session_key` | `str` | `None` | 网关会话密钥 |
| `user_id` | `str` | `None` | 用户 ID |
| `user_id_alt` | `str` | `None` | 备用用户 ID |
| `user_name` | `str` | `None` | 用户名 |
| `chat_id` | `str` | `None` | 聊天/频道 ID |
| `chat_name` | `str` | `None` | 聊天/频道名称 |
| `chat_type` | `str` | `None` | 聊天类型 |
| `thread_id` | `str` | `None` | 线程 ID |
| `platform` | `str` | `None` | 平台标识（如 slack, vscode 等） |
| `ephemeral_system_prompt` | `str` | `None` | 临时系统提示词 |
| `prefill_messages` | `List[Dict[str, Any]]` | `None` | 预填充消息列表 |

### 7. 记忆与上下文文件

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `skip_memory` | `bool` | `False` | 跳过记忆模块 |
| `skip_context_files` | `bool` | `False` | 跳过上下文文件（CLAUDE.md 等） |
| `load_soul_identity` | `bool` | `False` | 加载灵魂身份配置 |
| `session_db` | — | `None` | 会话数据库实例（用于记忆召回） |

### 8. 检查点

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `checkpoints_enabled` | `bool` | `False` | 是否启用检查点 |
| `checkpoint_max_snapshots` | `int` | 20 | 最大快照数 |
| `checkpoint_max_total_size_mb` | `int` | 500 | 检查点总大小上限（MB） |
| `checkpoint_max_file_size_mb` | `int` | 10 | 单文件大小上限（MB） |

### 9. 回调系统（16 个回调）

| 参数 | 类型 | 触发时机 |
|---|---|---|
| `tool_progress_callback` | `callable` | 工具执行进度更新 |
| `tool_start_callback` | `callable` | 工具开始执行 |
| `tool_complete_callback` | `callable` | 工具执行完成 |
| `thinking_callback` | `callable` | 模型思考过程 |
| `reasoning_callback` | `callable` | 模型推理过程 |
| `clarify_callback` | `callable` | Agent 需要澄清问题 |
| `read_terminal_callback` | `callable` | 需要读取终端 |
| `step_callback` | `callable` | 每个 agent 步骤 |
| `stream_delta_callback` | `callable` | 流式响应增量 |
| `interim_assistant_callback` | `callable` | 中间响应 |
| `tool_gen_callback` | `callable` | 工具生成 |
| `status_callback` | `callable` | 状态变更 |
| `notice_callback` | `callable` | 系统通知 |
| `notice_clear_callback` | `callable` | 清除通知 |
| `event_callback` | `Callable[[str, dict], None]` | 生命周期事件 |
| `reaction_callback` | `Callable[[str], None]` | 消息反应 |

### 10. 其他

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `credential_pool` | — | `None` | 凭证池（多租户/多用户场景） |

---

## 业务入口函数

### 1. `run_conversation()` — 核心入口

**[run_agent.py:5787-5810](run_agent.py#L5787-L5810)**

```python
def run_conversation(
    self,
    user_message: str,
    system_message: str = None,
    conversation_history: List[Dict[str, Any]] = None,
    task_id: str = None,
    stream_callback: Optional[callable] = None,
    persist_user_message: Optional[str] = None,
    persist_user_timestamp: Optional[float] = None,
    moa_config: Optional[dict[str, Any]] = None,
) -> Dict[str, Any]:
```

**最完整的对话入口**。转发到 `agent.conversation_loop.run_conversation()`。支持：

- **user_message**：用户输入消息
- **system_message**：可选的系统提示词
- **conversation_history**：历史对话记录
- **task_id**：任务标识，关联多个对话轮次
- **stream_callback**：流式输出回调
- **persist_user_message / persist_user_timestamp**：消息持久化
- **moa_config**：Mixture of Agents 多智能体配置

返回值是一个字典，核心字段包含 `final_response`。

---

### 2. `chat()` — 简化入口

**[run_agent.py:5812-5824](run_agent.py#L5812-L5824)**

```python
def chat(self, message: str, stream_callback: Optional[callable] = None) -> str:
```

**轻量级封装**，内部直接调用 `self.run_conversation(message, stream_callback=stream_callback)`，提取并返回 `result["final_response"]` 字符串。适合简单对话场景，不暴露完整响应结构。

---

### 3. `_run_codex_app_server_turn()` — Codex 运行时入口

**[run_agent.py:5826-5836](run_agent.py#L5826-L5836)**

```python
def _run_codex_app_server_turn(
    self, *,
    user_message: str,
    original_user_message: Any,
    messages: List[Dict[str, Any]],
    effective_task_id: str,
    should_review_memory: bool = False,
) -> Dict[str, Any]:
```

转发器，用于 Codex App Server 模式的单轮对话处理。实际逻辑在 `agent.codex_runtime.run_codex_app_server_turn()`。

---

### 4. `main()` — CLI 命令行入口

**[run_agent.py:5839](run_agent.py#L5839)**

```python
def main():
```

模块级函数（非类方法），`hermes-agent` CLI 工具的入口。负责：
1. 解析命令行参数
2. 构造 `AIAgent` 实例
3. 启动交互式会话循环

---

## 调用链路

```
CLI 入口:                     main()
                                │
                                ▼
                          AIAgent(...)         ← 70+ 构造参数 → init_agent()
                                │
              ┌─────────────────┼─────────────────┐
              ▼                 ▼                  ▼
      chat()              run_conversation()   _run_codex_app_server_turn()
      (简化)                (完整版)              (Codex 模式)
              │                 │                  │
              └─────────┬───────┘                  │
                        ▼                          ▼
            agent.conversation_loop    agent.codex_runtime
            .run_conversation()        .run_codex_app_server_turn()
```

## 设计特点

1. **纯门面模式**：`AIAgent` 不包含业务逻辑，所有方法都是转发器
2. **回调驱动架构**：16 个回调函数覆盖 agent 生命周期各阶段，前端可以灵活注入 UI 更新逻辑
3. **构造与行为分离**：`__init__` 只负责初始化配置，不执行任何对话操作
4. **base_url 属性**：有特殊的 property setter，同步维护 `_base_url_lower`（小写）和 `_base_url_hostname`（主机名）缓存
