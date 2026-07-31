# Hermes Agent 技能斜杠命令（/skill-name）启动流程

以 `/gif-search funny cats` 为例，追踪从用户输入到 LLM 接收的完整链路。

---

## 第1步：扫描技能并注册为斜杠命令

**文件：** [agent/skill_commands.py](../agent/skill_commands.py)

启动时，`scan_skill_commands()` 扫描磁盘上的技能目录（`~/.hermes/skills/` 及 `skills.external_dirs` 中配置的外部目录）：

1. 遍历每个目录，查找 **SKILL.md** 文件
2. 解析 **YAML 前置元数据**（`---` 分隔块），提取 `name` 和 `description`
3. 按优先级过滤：
   - 检查平台兼容性（`skill_matches_platform`）—— 不支持当前 OS 的技能被跳过
   - 检查运行时环境（`skill_matches_environment`）—— 不相关的技能被跳过
   - 检查用户禁用的技能列表（`skills.disabled` / `skills.platform_disabled`）
4. 名称标准化为连字符分隔的 slug：`"dogfood"` → `"/dogfood"`，`"gif search"` → `"/gif-search"`
5. 结果缓存到全局 `_skill_commands` 字典：
   ```
   {"/gif-search": {name, description, skill_md_path, skill_dir}, ...}
   ```

关键代码位置：
- `scan_skill_commands()` → `agent/skill_commands.py:320`
- 名称标准化 → `agent/skill_commands.py:372-377`

---

## 第2步：用户输入被 TUI 调度

**文件：** [cli.py](../cli.py)

当用户在 TUI 中输入 `/gif-search funny cats` 时：

```
cmd_lower = "/gif-search funny cats"
base_cmd  = "/gif-search"        # 按空格分割的第一个标记
rest      = "funny cats"         # 其余内容
```

调度发生在 `process_command()` 方法中（`cli.py:8911-9054`），按以下优先级匹配：

1. **内置命令**（`/help`、`/quit`、`/memory`、`/clear` 等）→ `cli.py:8852-8920`
2. **快速命令**（`config.yaml` 中用户定义的 `quick_commands`）→ `cli.py:8927-8968`
3. **插件命令**（`hermes_cli/plugins` 注册）→ `cli.py:8970-8985`
4. **技能包**（`/skill-bundle`）→ `cli.py:8988-9008`
5. **技能斜杠命令** ← `/gif-search` 在这里命中 → `cli.py:9011`

```python
# cli.py:9011
elif base_cmd in skill_commands:
    rest = cmd_original[len(base_cmd):].strip()
    ...
```

---

## 第3步：支持堆叠技能调用

**文件：** [agent/skill_commands.py](../agent/skill_commands.py)

在加载单个技能之前，先调用 `split_stacked_skill_commands(rest)` 检查用户是否堆叠了多个斜杠命令：

```
/gif-search /dogfood find me dog gifs
```

这遵循 Claude Code v2.1.199 的模式 —— 最多 5 个引导技能会被识别为技能命令，直到第一个非技能标记。剩余文本成为用户指令。

```python
# agent/skill_commands.py:553
def split_stacked_skill_commands(rest: str) -> tuple[list[str], str]:
    # 消费额外的 /skill 标记
    # 返回: (extra_cmd_keys, remaining_instruction)
```

对于单个 `/gif-search funny cats`，`extra_keys` 为空，走普通单技能分支。

关键代码位置：
- `split_stacked_skill_commands()` → `agent/skill_commands.py:553`
- 堆叠调用构建 → `build_stacked_skill_invocation_message()` → `agent/skill_commands.py:585`

---

## 第4步：加载技能并构建消息

**文件：** [agent/skill_commands.py](../agent/skill_commands.py)

### 4.1 加载技能

`build_skill_invocation_message("/gif-search", "funny cats")` 被调用（`agent/skill_commands.py:489`）：

1. 从 `skill_commands` 字典中查找技能信息
2. 调用 `_load_skill_payload()` 通过 `tools.skills_tool.skill_view()` 解析 SKILL.md，读取：
   - 完整的 markdown 内容
   - 前置元数据（name、description、version 等）
   - `linked_files`（引用的支持文件）
   - 设置状态（setup_needed、setup_note 等）
   - `skill_dir`（技能的绝对路径）
3. 跟踪技能使用（`tools.skill_usage.bump_use`），用于 Curator 生命周期管理

### 4.2 处理内容

`_build_skill_message()` 对技能内容执行以下处理：

1. **模板变量替换**（如果 `skills.template_vars` 启用）：
   - `{{template_vars}}` 被替换为实际值

2. **内联 Shell 展开**（如果 `skills.inline_shell` 启用）：
   - 反引号 shell 命令被执行，输出被替换

3. **注入技能目录路径**：
   - 让 agent 知道在哪里找到绑定的脚本

4. **注入技能配置值**（如果前置元数据中声明了 `metadata.hermes.config`）：
   - 从 `config.yaml` 解析的配置值被追加为 `[Skill config: ...]` 块

5. **设置说明**（如果技能需要环境设置）

6. **支持文件列表**：
   - 列出 `references/`、`templates/`、`scripts/`、`assets/` 目录中的文件
   - 告知 agent 如何通过 `skill_view()` 或绝对路径加载它们

### 4.3 最终消息格式

构建的最终消息是一个字符串，包含：

```
[IMPORTANT: The user has invoked the "gif-search" skill, indicating they want
you to follow its instructions. The full skill content is loaded below.]

<完整的 SKILL.md 内容放在这里...>

[Skill directory: /Users/song/.hermes/skills/gif-search/]
Resolve any relative paths in this skill (e.g. `scripts/foo.js`,
`templates/config.yaml`) against that directory...

[This skill has supporting files:]
- scripts/search.py  ->  /Users/song/.hermes/skills/gif-search/scripts/search.py
...

The user has provided the following instruction alongside the skill invocation: funny cats
```

关键代码位置：
- `build_skill_invocation_message()` → `agent/skill_commands.py:489`
- `_load_skill_payload()` → `agent/skill_commands.py:138`
- `_build_skill_message()` → `agent/skill_commands.py:217`

---

## 第5步：消息注入 LLM 对话

**文件：** [cli.py](../cli.py)

构建好的消息通过 `self._pending_input.put(msg)` 放入待处理输入队列（`cli.py:9052`）。

在下一个 LLM 回合中，该消息作为 **用户消息** 发送给模型。模型将完整的 SKILL.md 内容视为一条指令（"你应该按照这些指引操作"），并将 "funny cats" 视为用户的具���输入。

```python
# cli.py:9045-9052
msg = build_skill_invocation_message(
    base_cmd, user_instruction, task_id=self.session_id
)
if msg:
    skill_name = skill_commands[base_cmd]["name"]
    print(f"\n⚡ Loading skill: {skill_name}")
    if hasattr(self, '_pending_input'):
        self._pending_input.put(msg)
```

---

## 第6步：内存清理（事后钩子）

**文件：** [agent/skill_commands.py](../agent/skill_commands.py)

回合结束后，`extract_user_instruction_from_skill_message()` 从扩展的脚手架中恢复 **仅** `"funny cats"`。

这是为了避免内存提供者（mem0、openviking、hindsight、retaindb、byterover、honcho、supermemory 等）存储整个技能主体 —— 它们只需记住用户实际输入的内容。

```python
# agent/skill_commands.py:58
def extract_user_instruction_from_skill_message(content: Any) -> Optional[str]:
    """Recover the user's instruction from a slash-skill-expanded turn."""
    if not isinstance(content, str):
        return None
    if not content.startswith(_SKILL_INVOCATION_PREFIX):
        return content  # 普通消息，直接返回
    # 技能脚手架 → 提取用户指令
    ...
```

脚手架使用特定的标记（存放在 `agent/skill_commands.py:47-56`）来界定技能主体和用户指令：
- `_SKILL_INVOCATION_PREFIX` = `"[IMPORTANT: The user has invoked the "`
- `_SINGLE_SKILL_MARKER` = `"The full skill content is loaded below.]"`
- `_SINGLE_SKILL_INSTRUCTION` = `"The user has provided the following instruction alongside the skill invocation: "`

---

## 架构总结图

```
~/.hermes/skills/gif-search/SKILL.md
          │
          ▼
  scan_skill_commands()          ← 启动时 / 热重载时
          │
          ▼
  _skill_commands = {"/gif-search": {name, description, skill_md_path, skill_dir}}
          │
          ▼
  用户输入: "/gif-search funny cats"
          │
          ▼
  cli.py process_command()       ← TUI 分发
          │
          ├─ 内置命令？  否
          ├─ 快捷命令？  否
          ├─ 插件命令？  否
          ├─ 技能包？    否
          └─ 技能命令？  √ → base_cmd = "/gif-search"
                              rest      = "funny cats"
          │
          ▼
  build_skill_invocation_message("/gif-search", "funny cats")
          │
          ├─ _load_skill_payload()      从磁盘读取 SKILL.md
          ├─ 模板变量 / 内联 Shell 展开
          └─ _build_skill_message()      组装完整消息
          │
          ▼
  _pending_input.put(msg)        ← 注入 LLM 回合
          │
          ▼
  LLM 将 SKILL.md 内容视为系统指令
  将 "funny cats" 视为用户输入
          │
          ▼
  extract_user_instruction_from_skill_message()  ← 内存清理
          │
          └─ 内存提供者只存储 "funny cats"
```

---

## 涉及的关键文件

| 文件 | 角色 |
|------|------|
| `agent/skill_commands.py` | 技能扫描、消息构建、堆叠调用、内存清理 |
| `agent/skill_bundles.py` | 技能包（多技能组合） |
| `agent/skill_preprocessing.py` | 模板替换、内联 Shell 展开 |
| `agent/skill_utils.py` | 技能工具函数（标准化、前端元数据解析等） |
| `tools/skills_tool.py` | 技能视图（`skill_view`）、SKILLS_DIR、解析前端元数据 |
| `cli.py` | TUI 命令分发、`process_command()` |
| `cli.py:3553-3594` | `_ensure_skill_commands()` 等桥接函数 |
| `cli.py:8911-9054` | 斜杠命令调度逻辑 |
