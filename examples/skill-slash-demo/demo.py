#!/usr/bin/env python3
"""
Skill Slash Command Demo — 用 LangChain + LangGraph 复刻 Hermes Agent 的
/技能名 斜杠命令机制。

Usage:
    python demo.py "/gif-search funny cats"
    python demo.py "/dogfood https://example.com login flow"
    python demo.py "/code-review def foo(): pass"
    python demo.py --list              # 列出可用技能
    python demo.py --dry-run "/gif-search cats"  # 只构建 prompt，不调 LLM

Environment:
    OPENAI_API_KEY  你的 OpenAI API key
    OPENAI_BASE_URL 可选，自定义 API 端点（兼容接口）
"""

import argparse
import os
import sys
import re
import yaml
from pathlib import Path
from typing import Optional, TypedDict

# LangChain / LangGraph 采用懒加载 —— 只在 cmd_invoke() 真正需要 LLM 时才导入。
# --list 和 --dry-run 模式不需要 LLM，避免 langgraph 的重载初始化开销。

# ============================================================================
# 第1步：扫描技能 —— 等价于 Hermes 的 scan_skill_commands()
# ============================================================================

SKILLS_DIR = Path(__file__).resolve().parent / "skills"

# 用于解析 YAML frontmatter（--- ... ---）
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_frontmatter_and_body(content: str) -> tuple[dict, str]:
    """解析 SKILL.md 的 YAML 前置元数据和 body。"""
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return {}, content
    frontmatter = yaml.safe_load(m.group(1)) or {}
    body = content[m.end():]
    return frontmatter, body


def _slugify(name: str) -> str:
    """将技能名标准化为 /slug 格式。"""
    slug = name.lower().replace(" ", "-").replace("_", "-")
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return f"/{slug}"


def scan_skills(skills_dir: Path = SKILLS_DIR) -> dict:
    """
    扫描 skills_dir 下所有子目录中的 SKILL.md，返回:
      {"/slug": {"name": ..., "description": ..., "path": ...}, ...}
    """
    commands: dict = {}
    if not skills_dir.exists():
        return commands

    for skill_md in sorted(skills_dir.rglob("SKILL.md")):
        # 跳过隐藏目录
        if any(part.startswith(".") for part in skill_md.parts):
            continue
        try:
            content = skill_md.read_text(encoding="utf-8")
            frontmatter, body = _parse_frontmatter_and_body(content)
            name = frontmatter.get("name", skill_md.parent.name)
            description = frontmatter.get("description", "")
            if not description:
                # 从 body 第一行取描述
                for line in body.strip().split("\n"):
                    line = line.strip()
                    if line and not line.startswith("#"):
                        description = line[:80]
                        break
            slug = _slugify(name)
            commands[slug] = {
                "name": name,
                "description": description or f"Invoke the {name} skill",
                "path": str(skill_md),
                "dir": str(skill_md.parent),
                "body": body.strip(),
            }
        except Exception as e:
            print(f"[warn] Failed to parse {skill_md}: {e}", file=sys.stderr)
    return commands


# ============================================================================
# 第2步：构建技能调用消息 —— 等价于 Hermes 的 build_skill_invocation_message()
# ============================================================================

_SKILL_INVOCATION_PREFIX = (
    "[IMPORTANT: The user has invoked the \"{name}\" skill, indicating they want "
    "you to follow its instructions. The full skill content is loaded below.]"
)
_SKILL_DIR_NOTE = (
    "\n\n[Skill directory: {dir}]\n"
    "Resolve any relative paths in this skill against that directory."
)
_USER_INSTRUCTION_LINE = (
    "\n\nThe user has provided the following instruction alongside "
    "the skill invocation: {instruction}"
)


def build_skill_message(
    skill_info: dict,
    user_instruction: str = "",
) -> str:
    """
    构建发送给 LLM 的完整消息 —— 技能主体 + 用户指令。
    等价于 Hermes agent/skill_commands.py 的 _build_skill_message()。
    """
    parts = [
        _SKILL_INVOCATION_PREFIX.format(name=skill_info["name"]),
        "",
        skill_info["body"],
        _SKILL_DIR_NOTE.format(dir=skill_info["dir"]),
    ]
    if user_instruction:
        parts.append(
            _USER_INSTRUCTION_LINE.format(instruction=user_instruction)
        )
    return "\n".join(parts)


# ============================================================================
# 第3步：LangGraph 流水线 —— 扫描 → 匹配 → 构建 → LLM 调用
# ============================================================================

class SkillState(TypedDict):
    """LangGraph 状态：贯穿所有节点的数据。"""
    raw_input: str               # 用户输入的完整命令行
    skill_slug: str              # 匹配到的技能 slug，如 /gif-search
    user_instruction: str        # 技能名后面的用户指令
    skill_info: dict             # 技能元信息（name, description, body, dir）
    skill_message: str           # 构建好的完整 prompt
    response: str                # LLM 返回结果
    error: str                   # 错误信息


def node_parse_input(state: SkillState) -> SkillState:
    """
    节点1：解析用户输入。
    "/gif-search funny cats" → slug="/gif-search", instruction="funny cats"
    """
    raw = state["raw_input"].strip()
    if not raw.startswith("/"):
        return {**state, "error": "命令必须以 / 开头，例如 /gif-search cats"}

    parts = raw.split(None, 1)
    slug = parts[0].lower()
    instruction = parts[1] if len(parts) > 1 else ""
    return {**state, "skill_slug": slug, "user_instruction": instruction}


def node_load_skill(state: SkillState) -> SkillState:
    """
    节点2：加载技能 —— 等价于 _load_skill_payload()。
    """
    if state.get("error"):
        return state

    commands = scan_skills()
    skill_info = commands.get(state["skill_slug"])
    if not skill_info:
        # 尝试前缀匹配
        matches = [k for k in commands if k.startswith(state["skill_slug"])]
        if len(matches) == 1:
            skill_info = commands[matches[0]]
            state["skill_slug"] = matches[0]

    if not skill_info:
        available = "\n  ".join(commands.keys()) if commands else "(none)"
        return {
            **state,
            "error": (
                f"未知命令: {state['skill_slug']}\n"
                f"可用技能:\n  {available}\n"
                f"使用 --list 查看详细信息"
            ),
        }
    return {**state, "skill_info": skill_info}


def node_build_message(state: SkillState) -> SkillState:
    """
    节点3：构建消息 —— 等价于 _build_skill_message()。
    """
    if state.get("error"):
        return state

    msg = build_skill_message(state["skill_info"], state["user_instruction"])
    return {**state, "skill_message": msg}


def node_call_llm(state: SkillState) -> SkillState:
    """
    节点4：调用 LLM —— 使用 LangChain ChatOpenAI。
    懒加载 langchain 相关模块，避免 --list / --dry-run 时不需要的开销。
    """
    if state.get("error"):
        return state

    from langchain_openai import ChatOpenAI
    from langchain_core.messages import HumanMessage, SystemMessage

    llm = ChatOpenAI(
        model=os.getenv("SKILL_DEMO_MODEL", "gpt-4o-mini"),
        temperature=0.7,
        max_tokens=1024,
    )

    messages = [
        SystemMessage(content="You are a helpful assistant. Follow the skill instructions provided in the user message."),
        HumanMessage(content=state["skill_message"]),
    ]

    try:
        result = llm.invoke(messages)
        content = result.content if hasattr(result, "content") else str(result)
        return {**state, "response": content}
    except Exception as e:
        return {**state, "error": f"LLM 调用失败: {e}"}


def build_graph():
    """构建 LangGraph 流水线。懒加载避免影响 --list / --dry-run。"""
    from langgraph.graph import StateGraph, END

    graph = StateGraph(SkillState)

    graph.add_node("parse_input", node_parse_input)
    graph.add_node("load_skill", node_load_skill)
    graph.add_node("build_message", node_build_message)
    graph.add_node("call_llm", node_call_llm)

    # 定义边：线性流水线
    graph.set_entry_point("parse_input")
    graph.add_edge("parse_input", "load_skill")
    graph.add_edge("load_skill", "build_message")
    graph.add_edge("build_message", "call_llm")
    graph.add_edge("call_llm", END)

    return graph.compile()


# ============================================================================
# CLI 入口
# ============================================================================

def cmd_list():
    """列出所有可用技能。"""
    commands = scan_skills()
    if not commands:
        print("没有找到技能。请在 skills/ 目录下放置 SKILL.md 文件。")
        return

    print(f"\n{'='*60}")
    print(f"  可用技能 ({len(commands)} 个)")
    print(f"{'='*60}\n")
    for slug, info in sorted(commands.items()):
        print(f"  {slug}")
        print(f"    名称: {info['name']}")
        print(f"    描述: {info['description']}")
        print(f"    路径: {info['path']}")
        print()


def cmd_dry_run(raw_input: str):
    """只构建 prompt，不调 LLM —— 方便调试。"""
    commands = scan_skills()
    parts = raw_input.strip().split(None, 1)
    slug = parts[0].lower()
    instruction = parts[1] if len(parts) > 1 else ""

    skill_info = commands.get(slug)
    if not skill_info:
        # 前缀匹配
        matches = [k for k in commands if k.startswith(slug)]
        if len(matches) == 1:
            skill_info = commands[matches[0]]
            slug = matches[0]

    if not skill_info:
        print(f"未知命令: {slug}")
        print(f"可用: {', '.join(commands.keys())}")
        return

    msg = build_skill_message(skill_info, instruction)
    print(f"\n{'='*60}")
    print(f"  构建的消息 (将发送给 LLM)")
    print(f"{'='*60}\n")
    print(msg)
    print(f"\n{'='*60}")
    print(f"  消息长度: {len(msg)} 字符")
    print(f"{'='*60}\n")


def cmd_invoke(raw_input: str):
    """完整流程：解析 → 加载 → 构建 → LLM。"""
    graph = build_graph()

    print(f"\n⚡ 正在处理: {raw_input}\n")

    # 执行 LangGraph 流水线
    initial_state: SkillState = {
        "raw_input": raw_input,
        "skill_slug": "",
        "user_instruction": "",
        "skill_info": {},
        "skill_message": "",
        "response": "",
        "error": "",
    }

    result = graph.invoke(initial_state)

    # 输出结果
    if result.get("error"):
        print(f"❌ 错误: {result['error']}")
        return

    skill_name = result["skill_info"]["name"]
    instruction = result["user_instruction"]

    print(f"技能: {skill_name}")
    if instruction:
        print(f"指令: {instruction}")
    print(f"{'─'*60}\n")
    print(result["response"])
    print(f"\n{'─'*60}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Skill Slash Command Demo — LangChain + LangGraph",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python demo.py "/gif-search funny cats"
  python demo.py "/dogfood https://example.com"
  python demo.py --list
  python demo.py --dry-run "/code-review def foo(): pass"
        """,
    )
    parser.add_argument(
        "command", nargs="?", default="",
        help="斜杠命令，如 '/gif-search funny cats'"
    )
    parser.add_argument(
        "--list", action="store_true",
        help="列出所有可用技能"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只构建 prompt 不调用 LLM（调试用）"
    )
    args = parser.parse_args()

    if args.list:
        cmd_list()
    elif not args.command:
        parser.print_help()
        print("\n提示: 使用 --list 查看可用技能")
    elif args.dry_run:
        cmd_dry_run(args.command)
    else:
        cmd_invoke(args.command)
