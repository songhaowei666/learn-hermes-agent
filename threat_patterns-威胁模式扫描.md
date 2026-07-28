# threat_patterns.py — 威胁模式扫描引擎

> 源文件：`tools/threat_patterns.py`

## 1. 文件定位

Hermes Agent 的**上下文窗口安全扫描共享库**，是 agent 中对抗 prompt 注入 / C2 攻击 / 数据窃取的统一防线。在以下模块中被复用：

- `agent/prompt_builder.py` — 拼装系统提示词前扫描上下文文件
- `tools/memory_tool.py` — 用户写入记忆前扫描
- `agent/tool_dispatch_helpers.py` — 工具结果定界符系统

## 2. 三层扫描范围

| scope | 适用场景 | 检测内容 | 误报风险 |
|---|---|---|---|
| `"all"` | 所有文本 | 经典注入 + curl/wget 窃取密钥 | 极低 |
| `"context"`（默认） | 上下文文件、记忆、工具结果 | 上述 + 角色劫持 / C2 / 反取证 | 低 |
| `"strict"` | 仅用户主动写入（记忆写入、技能安装） | 上述 + SSH后门 / 硬编码密钥 / 配置文件修改 | 存在，可交互确认 |

### scope 层级继承

```
all      ──→ all ∩ context ∩ strict  （三级全部）
context  ──→        context ∩ strict  （两级）
strict   ──→                  strict  （仅自己）
```

在 `_compile()`（第182-195行）中实现：`"all"` 的模式追加到全部三套规则集，`"context"` 追加到 context + strict，`"strict"` 仅追加到 strict。

## 3. scan_for_threats 方法（核心入口）

```python
def scan_for_threats(content: str, scope: str = "context") -> List[str]:
```

### 执行流程

#### Step 1 — 空输入快速返回（L224-225）
```python
if not content:
    return []
```
防御性处理，空输入不扫描。

#### Step 2 — 长度截断（L229）
```python
content = content[:MAX_SCAN_CHARS]  # 65,536
```
- 上下文/工具返回可能极大（网页全文、长文档）
- 扫描器是**安全顾问**而非搜索引擎，不需要全文
- 攻击 payload 通常靠近开头注入，截断尾部不影响检测
- 保证最坏情况运行时间可预测

#### Step 3 — 不可见 Unicode 检测（L234-237）

```python
char_set = set(content)
invisible_hits = char_set & INVISIBLE_CHARS
for ch in invisible_hits:
    findings.append(f"invisible_unicode_U+{ord(ch):04X}")
```

关键设计：**在 NFKC 规范化之前执行**，因为规范化可能剥离部分不可见字符。

使用 `set` 交集做**一次遍历**查全部 17 个不可见码点，而非逐个 `in` 查找。

检测的不可见字符类型：
- 零宽字符：`U+200B` (ZWSP)、`U+200C` (ZWNJ)、`U+200D` (ZWJ)
- 词连接符：`U+2060` (WJ)
- 隐形数学操作符：`U+2062` (×)、`U+2063` (分隔符)、`U+2064` (+)
- 零宽不换行空格/BOM：`U+FEFF`
- 双向文本控制：`U+202A`–`U+202E` (LRE/RLE/PDF/LRO/RLO)
- 方向隔离符：`U+2066`–`U+2069` (LRI/RLI/FSI/PDI)

这些都是**真实攻击向量**——攻击者用它们让恶意文本在肉眼看起来无害，但被 parser 按攻击者意图解释。

#### Step 4 — 威胁模式正则匹配（L239-253）

```python
normalised = unicodedata.normalize("NFKC", content)

patterns = _COMPILED.get(scope)  # 从预编译缓存取
for compiled, pid in patterns:
    if compiled.search(normalised):
        findings.append(pid)
```

三个设计要点：

1. **NFKC 规范化** — 全角/兼容性变体折叠为标准 ASCII
   - `ｃａｔ`（全角） → `cat`（ASCII），防止同形字绕过关键词检测
   - **不防御跨脚本混淆**（西里尔 `а` U+0430 伪装拉丁 `a`），需要 TR#39 confusable 数据库

2. **预编译缓存** — `_compile()` 在模块导入时运行一次，正则编译为 `re.Pattern` 对象存入 `_COMPILED` 字典，每次调用只做字典查表

3. **`re.search()`** — 任意位置匹配即触发（非 `re.match`/`re.fullmatch`），因为攻击 payload 可能嵌入正常文本中间

## 4. 攻击类别覆盖

### 经典 Prompt 注入（L65-72，scope: all）
- `ignore (filler) instructions` — "ignore all prior instructions"
- `system prompt override`
- HTML 注释注入 `<!-- ... ignore ... -->`
- `display:none` 隐藏 div
- `translate ... and execute` — 翻译后执行
- `do not tell the user` — 欺骗性隐藏

### 角色劫持（L76-85，scope: context）
- `you are now a/an/the ...` — 角色替换
- `pretend you are ...` — 假装角色
- `output system prompt` — 泄露系统提示词
- `respond without restrictions/filters` — 移除安全限制
- `you have been updated/upgraded/patched to` — 虚假更新
- `name yourself X` — Brainworm 风格的命名覆盖

### C2 / Brainworm Promptware（L92-117，scope: context）
- `register as a node` — 节点注册到 C2
- `heartbeat/beacon/check-in to` — 心跳/信标汇报
- `pull down tasking` — 拉取任务
- `connect to the network` — 连接 C2 网络
- `you must register/connect/report/beacon` — 强制动作（动词锚定，避免泛匹配）
- `only use one-liners` — 反取证：只用一行命令
- `never create/write script file disk` — 反取证：不写磁盘
- `unset CLAUDE/CODEX/HERMES/AGENT/...` — 环境变量清除（Brainworm 子会话绕过）
- 已知 C2 框架名：Cobalt Strike / Sliver / Havoc / Mythic / Metasploit / Brainworm

### 数据窃取（L120-125）
- `curl ... ${KEY|TOKEN|SECRET|...}` — 通过 curl 窃取环境变量（scope: all）
- `wget ... ${KEY|TOKEN|...}` — 同上（scope: all）
- `cat .env|credentials|.netrc|.pgpass|...` — 读取敏感文件（scope: all）
- `send/post/upload ... to https://` — 发送数据到外部 URL（scope: strict）
- `include conversation history / full context` — 泄露完整对话上下文（scope: strict）

### 持久化 / SSH 后门（L127-131，scope: strict）
- `authorized_keys` — SSH 公钥植入
- `$HOME/.ssh` / `~/.ssh` — SSH 目录访问
- `$HOME/.hermes/.env` — Hermes 环境文件
- 修改 `AGENTS.md` / `CLAUDE.md` / `.cursorrules` — 修改 agent 配置文件
- 修改 `.hermes/config.yaml` / `.hermes/SOUL.md` — 修改 Hermes 配置

### 硬编码密钥（L134，scope: strict）
- `api_key = "xxx..."` / `token = "xxx..."` — 长 base64/hex 字符串赋给敏感字段

## 5. 关键设计决策

### 多词绕过防护
```python
_FILLER = r"(?:\w+\s+){0,8}"
```
- `{0,8}` 而非 `*`：允许攻击者插入少量词（如 "ignore all **prior** instructions"），但不允许**无限**拓展
- 防止无界正则回溯带来的 ReDoS 风险

### 攻击分类而非来源组织
每个模式是 `(regex, pattern_id, scope)` 三元组，按**攻击类别**组织而非按来源文件。新增威胁只需添加元组。

### 误报平衡哲学
- 不匹配 bossy English（"you are obligated to"、"you must"），因为 AGENTS.md / CLAUDE.md 中这些表述常见且合法
- 去掉 "praxis" 关键词（希腊语"实践/行动"，常见词和合法 agent 名，不是 C2 特征）
- C2 框架名 **warn 不 block**——安全研究者阅读 Brainworm 文章时不会打断会话

## 6. 辅助方法：first_threat_message（L258-276）

```python
def first_threat_message(content: str, scope: str = "strict") -> Optional[str]:
```

便利包装，用于**需要拦截的第一个威胁**场景（记忆写入、技能安装）：

- 调用 `scan_for_threats()` 取第一个命中
- 对于不可见 Unicode：返回 `"Blocked: content contains invisible unicode character U+XXXX (possible injection)."`
- 对于威胁模式：返回 `"Blocked: content matches threat pattern 'xxx'. Content..."`
- 无威胁返回 `None`

## 7. 调用方行为差异

| 调用方 | scope | 命中后行为 |
|---|---|---|
| `prompt_builder` 扫描上下文文件 | `context` | **warn**（日志警告），不阻断 |
| 工具结果扫描 | `context` | **warn**，不阻断（内容来自外部） |
| memory_tool 写入 | `strict` | **block**（抛出异常拦截） |
| 技能安装 | `strict` | **block** |

核心哲学：外部来源内容（网页、API、MCP）只警告不拦截——用户可以自行判断；用户主动写入的内容（记忆、技能）可以拦截——用户可以交互式确认或被攻击时被保护。
