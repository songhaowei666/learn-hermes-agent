# AIAgent是hermes项目唯一的agent运行时

大致有50个参数，分类大致如下

## 连接/认证类：base_url, api_key, provider, api_mode, acp_command, command

## 模型配置类：model, max_tokens, reasoning_config, service_tier

## 行为控制类：max_iterations, tool_delay, enabled_toolsets, disabled_toolsets

## 回调/扩展点类：*_callback 系列的十几个回调

## 调试/日志类：verbose_logging, save_trajectories, log_prefix 等

## Provider 路由类：providers_allowed, providers_order，openrouter_min_coding_score 等