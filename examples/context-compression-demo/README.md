# Context Compression Demo

最小化的上下文压缩引擎——剥离了 hermes-agent 的 SQLite / gateway / 插件等基础设施，只保留核心的**五步压缩算法**和**真实的提示词模板**。

## 文件

| 文件 | 用途 |
|---|---|
| `mini_compressor.py` | 压缩引擎主程序 |
| `example_conversation.json` | 示例对话数据（中文，HN 爬虫场景） |

## 快速开始

```bash
cd context-compression-demo

# 1. 安装依赖
pip install openai

# 2. 设置 API key（支持任意 OpenAI-compatible 接口）
export OPENAI_API_KEY="sk-..."
# 可选：自定义 base_url
export OPENAI_BASE_URL="https://api.openai.com/v1"

# 3. 运行内置 demo
python mini_compressor.py

# 4. 使用自定义对话文件
python mini_compressor.py --input example_conversation.json

# 5. 干跑模式（只看 split，不调用 LLM）
python mini_compressor.py --input example_conversation.json --dry-run

# 6. 指定模型
python mini_compressor.py --model deepseek-chat --base-url https://api.deepseek.com/v1
```

## 运行效果

```
╔══════════════════════════════════════════════════╗
║   Mini Context Compressor — Demo                 ║
║   Using real hermes-agent prompt templates       ║
╚══════════════════════════════════════════════════╝

============================================================
  ORIGINAL CONVERSATION: 19 messages
============================================================
  [ 0] system      | You are a helpful coding assistant...
  [ 1] user        | 帮我写一个 Python 爬虫脚本...
  [ 2] assistant   | [1 tool calls] | 好的，让我先看一下...
  [ 3] tool        | requests 2.31.0, beautifulsoup4 4.12.3...
  [ 4] user        | 好，直接写吧，要能处理分页...
  ... (省略)
  [18] tool        | total 32 / hn_scraper.py / requirements.txt...

⚡ Token threshold exceeded — triggering compression...

[Step 1] Pruning tool results (input: 19 messages)...
         Pruned 3 tool result(s)
[Step 2-3] Determining head / middle / tail boundaries...
         head=4  middle=11  tail=4
[Step 4] Generating summary for 11 middle messages...
         Summary generated (1347 chars)
[Step 5] Done. 19 messages → 9 messages (summary replaces 11 middle turns)

============================================================
  COMPRESSED CONVERSATION: 9 messages
============================================================
  [ 0] system      | You are a helpful coding assistant...
  [ 1] user        | 帮我写一个 Python 爬虫脚本...
  [ 2] assistant   | [1 tool calls] | 好的，让我先看一下...
  [ 3] tool        | requests 2.31.0, beautifulsoup4 4.12.3...
  [ 4] user 🗜️     | [CONTEXT COMPACTION — REFERENCE ONLY]...
  [ 5] user        | 等一下，再加个功能...
  [ 6] assistant   | [1 tool calls] | 好的，加上 JSON 导出和排序...
  [ 7] user        | 好，现在帮我看看这个项目的目录结构...
  [ 8] assistant   | [1 tool calls] | ...
```

## 对应关系

demo 中每个类/方法对应 hermes-agent 中的哪个：

| demo | 原始代码 | 说明 |
|---|---|---|
| `MiniContextEngine` | `agent/context_engine.py:ContextEngine` | 抽象基类 |
| `MiniCompressor` | `agent/context_compressor.py:ContextCompressor` | 默认压缩引擎 |
| `compress()` | `ContextCompressor.compress()` | 五步压缩入口 |
| `_prune_old_tool_results()` | 同名方法 | 工具结果裁剪 |
| `_split_head_middle_tail()` | `compress()` 内联逻辑 | 头尾保护分界 |
| `_generate_summary()` | 同名方法 | LLM 摘要生成 |
| `_deterministic_fallback()` | 同名方法 | LLM 不可用时的兜底 |
| `_build_summary_message()` | 内联 | 嵌入 `SUMMARY_PREFIX` |
| `_automatic_compression_blocked()` | 同名方法 | 守门员检查 |
| `SUMMARY_PREFIX` | 第 44 行常量 | 真实提示词 |
| `HISTORICAL_*_HEADING` | 第 38-41 行常量 | 真实模板标题 |
| `_summarizer_preamble` | `_generate_summary()` 内 | 真实 summarizer 指令 |
| `_template_sections` | `_generate_summary()` 内 | 真实结构化模板 |

## 算法流程

```
                    ┌──────────┐
                    │ messages │  (19条)
                    └────┬─────┘
          ┌──────────────┴──────────────┐
          │ Step 1: Prune Old Tool      │
          │   Results (no LLM call)     │
          │   - deduplicate identical   │
          │   - one-line summaries      │
          └──────────────┬──────────────┘
          ┌──────────────┴──────────────┐
          │ Step 2-3: Split             │
          │   head (protected)          │
          │   middle (compressible)     │
          │   tail (protected by token  │
          │         budget)             │
          └──────────────┬──────────────┘
          ┌──────────────┴──────────────┐
          │ Step 4: LLM Summary         │
          │   - first time: full prompt │
          │   - later: iterative update │
          │   - fallback if LLM fails   │
          └──────────────┬──────────────┘
          ┌──────────────┴──────────────┐
          │ Step 5: Assemble            │
          │   head + [summary] + tail   │
          └──────────────┘
```

## 反抖动机制

```python
# 条件一：冷却期（LLM 调用失败后 60s 不重试）
if self._summary_failure_cooldown_until - time.monotonic() > 0:
    return True

# 条件二：连续无效（两次压缩都没腾出空间）
if self._ineffective_compression_count >= 2:
    return True

# 条件三：连续兜底（两次都走了 fallback）
if self._fallback_compression_streak >= 2:
    return True
```

手动调用 `compress(force=True)` 可绕过守门员。
