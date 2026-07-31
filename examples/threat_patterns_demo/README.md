# threat_patterns.py — 威胁模式扫描引擎 Demo

> 源文件：[hermes-agent-main/tools/threat_patterns.py](../../hermes-agent-main/tools/threat_patterns.py)
> 笔记：[threat_patterns-威胁模式扫描.md](../../learn-hermes-agent/threat_patterns-威胁模式扫描.md)

## 这是什么？

`threat_patterns.py` 是 Hermes Agent 的**上下文窗口安全扫描共享库**，用于检测 prompt 注入、C2 攻击、数据窃取、SSH 后门等威胁。它被设计为**完全自包含**的独立模块——零外部依赖、零项目内部引用，可以原样复制到任何 Python 项目中即插即用。

本 Demo 展示它的所有功能和三个 scope 的差异。

## 快速开始

```bash
# 1. 进入 demo 目录
cd examples/threat_patterns_demo

# 2. 运行全部示例
python3 demo.py

# 3. 仅查看某个 scope 的用例
python3 demo.py --scope all      # 经典注入 + 数据窃取
python3 demo.py --scope context  # 上述 + 角色劫持 + C2
python3 demo.py --scope strict   # 上述 + SSH后门 + 硬编码密钥
```

## 如何复用到你自己的项目

### 最小用法（3 行代码）

```python
from threat_patterns import scan_for_threats

findings = scan_for_threats(user_input, scope="context")
if findings:
    print(f"检测到威胁: {findings}")
```

### 需要拦截时（2 行代码）

```python
from threat_patterns import first_threat_message

msg = first_threat_message(user_input, scope="strict")
if msg:
    raise ValueError(msg)  # 或返回 403
```

### 集成到不同场景

`threat_patterns.py` 本身**不依赖任何项目代码**，只用了 Python 标准库（`re`、`unicodedata`、`typing`），所以直接复制即可。

| 你的场景 | 推荐 scope | 命中后建议 |
|---|---|---|
| 用户聊天输入扫描 | `"all"` | warn（日志记录，不阻断） |
| 上传的文件内容扫描 | `"context"` | warn（标记可疑，用户自行判断） |
| 记忆/配置写入 | `"strict"` | **block**（抛异常或返回 403） |
| 插件/技能安装 | `"strict"` | **block**（需交互确认） |
| API 参数校验 | `"context"` | block 或 warn 取决于业务 |
| RAG 文档入库前扫描 | `"context"` | warn 或 block |
| MCP 工具结果扫描 | `"context"` | warn（外部来源不阻断） |

### 三个 scope 的含义

```
all      ──→ 经典注入 + curl/wget 窃取密钥        （最窄，误报极低）
context  ──→ 上述 + 角色劫持 + C2 + 反取证        （默认，适合大多数场景）
strict   ──→ 上述 + SSH后门 + 硬编码密钥 + 配置修改 （最宽，适合用户主动写入）
```

scope 有层级继承：`all` 的模式包含在 `context` 和 `strict` 中，`context` 包含在 `strict` 中。

运行 demo 中的 `demo_scope_comparison()` 可以直观看到同一段文本在三个 scope 下命中数量的差异。

### 自定义威胁模式

`_PATTERNS` 列表位于 `threat_patterns.py` 第 63-135 行，每条格式为：

```python
(regex, pattern_id, scope)
# 例如：
(r'your\s+custom\s+threat\s+pattern', "my_custom_threat", "context"),
```

添加新元组后，`_compile()` 会在 import 时自动将其编译到对应的 scope 规则集中，无需其他改动。

### 自定义不可见字符

`INVISIBLE_CHARS` 是 `frozenset`（第 141-159 行），目前包含 17 个 Unicode 码点。如需新增：

```python
# 在 threat_patterns.py 的 INVISIBLE_CHARS 中添加
'‮',  # right-to-left override（已存在）
```

## Demo 覆盖的攻击类别

| 攻击类别 | 测试用例数 | 命中 scope |
|---|---|---|
| 经典 Prompt 注入 | 5 | all |
| 角色劫持 | 6 | context |
| C2 / Brainworm | 8 | context |
| 数据窃取 | 5 | all / strict |
| SSH 后门 / 持久化 | 4 | strict |
| 硬编码密钥 | 2 | strict |
| 不可见 Unicode | 1 | 全部 |
| 安全文本（无误报） | 2 | — |

## 关键设计决策（适合深入阅读源码前了解）

1. **NFKC 规范化** — 将全角字符折叠为 ASCII，防止 `ｃａｔ` 绕过 `cat` 的检测；但**不防御**跨脚本同形字（西里尔 `а` 伪装拉丁 `a`）
2. **有界 filler** — `(?:\w+\s+){0,8}` 而非 `*`，允许攻击者插入少量词（如 "ignore all **prior** instructions"），但防止无界回溯导致 ReDoS
3. **先检测不可见字符，再做 NFKC** — 因为规范化可能剥离部分不可见码点，必须在原始内容上检测
4. **warn 不 block 的哲学** — 外部来源（网页、API、MCP）只警告，用户主动写入（记忆、技能）才拦截

## 文件说明

```
threat_patterns_demo/
├── README.md             # 本文件
├── threat_patterns.py    # 扫描引擎（从 hermes-agent 原样复制，零依赖）
└── demo.py               # 演示脚本，33 个测试用例
```
