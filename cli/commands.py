#!/usr/bin/env python3
"""CLI 命令处理模块"""

import asyncio
import uuid

from src.agents.stock_agent import StockAnalysisAgent

# ============================================================
# 提示文本
# ============================================================

TIPS = """💡 提示:
   - 输入问题后按回车，Agent 会流式回复
   - 输入 /new [id] 创建新会话
   - 输入 /history 查看对话历史
   - 输入 /threads 查看所有会话
   - 输入 /switch <id> 切换会话
   - 输入 /clear 清空当前会话
   - 输入 /exit 退出程序"""

COMMAND_HELP = """
📖 命令帮助:
─────────────────────────────────────────
  /new [id]     - 创建新会话（可选指定 ID）
  /history      - 查看当前会话历史
  /threads      - 查看所有会话列表
  /switch <id>  - 切换到指定会话
  /clear        - 清空当前会话
  /exit         - 退出程序
  /help         - 显示此帮助

💡 使用技巧:
  - 输入后直接回车即可发送（支持多行粘贴）
  - 多行输入：按两次回车（空行）结束多行模式
  - 输入 '.' 再回车取消当前输入
─────────────────────────────────────────
"""


# ============================================================
# 命令路由
# ============================================================

async def handle_command(
    command: str, agent: StockAnalysisAgent, thread_id: str
) -> tuple[str, bool]:
    """路由命令到对应处理函数，返回 (new_thread_id, running)"""
    cmd = command.lower().strip()

    if cmd == "/exit" or cmd == "/quit":
        return await _cmd_exit(thread_id)

    elif cmd == "/new" or cmd.startswith("/new "):
        return await _cmd_new(cmd, agent, thread_id)

    elif cmd == "/history":
        return await _cmd_history(agent, thread_id)

    elif cmd == "/threads":
        return await _cmd_threads(agent, thread_id)

    elif cmd.startswith("/switch"):
        return await _cmd_switch(cmd, thread_id)

    elif cmd == "/clear":
        return await _cmd_clear(agent, thread_id)

    elif cmd == "/help":
        return _cmd_help(thread_id)

    else:
        print(f"\n⚠️ 未知命令: {command}，输入 /help 查看帮助")
        return thread_id, True


# ============================================================
# 命令实现
# ============================================================

async def _cmd_new(cmd: str, agent: StockAnalysisAgent, thread_id: str) -> tuple[str, bool]:
    parts = cmd.split(maxsplit=1)
    if len(parts) >= 2:
        new_id = parts[1].strip()
        existing = await agent.list_threads()
        if new_id in existing:
            print(f"\n❌ 会话 '{new_id}' 已存在，请使用 /switch {new_id} 切换")
            return thread_id, True
    else:
        new_id = str(uuid.uuid4())[:12]
    print(f"\n✅ 已创建新会话: {new_id}")
    return new_id, True


async def _cmd_exit(thread_id: str) -> tuple[str, bool]:
    return thread_id, False


async def _cmd_history(agent: StockAnalysisAgent, thread_id: str) -> tuple[str, bool]:
    print("\n📜 对话历史:")
    print("-" * 40)

    history = await agent.get_thread_history(thread_id)

    if not history:
        print("  暂无历史记录")
        return thread_id, True

    for msg in history:
        role_map = {"human": "👤 用户", "ai": "🤖 助手", "tool": "🔧 工具"}
        role = role_map.get(getattr(msg, "type", ""), "❓ 未知")

        content = getattr(msg, "content", str(msg))
        if isinstance(content, str):
            if len(content) > 200:
                content = content[:200] + "..."
            if role == "🔧 工具" and len(content) > 100:
                content = content[:100] + "..."
            print(f"  {role}: {content}")
        else:
            print(f"  {role}: {type(content)}")

    return thread_id, True


async def _cmd_threads(agent: StockAnalysisAgent, thread_id: str) -> tuple[str, bool]:
    print("\n📋 所有会话:")
    print("-" * 40)

    threads = await agent.list_threads()

    if not threads:
        print("  暂无会话记录")
        return thread_id, True

    for tid in threads:
        marker = "👉 " if tid == thread_id else "   "
        print(f"{marker}{tid}")

    return thread_id, True


async def _cmd_switch(cmd: str, thread_id: str) -> tuple[str, bool]:
    parts = cmd.split()
    if len(parts) >= 2:
        new_id = parts[1]
        print(f"\n✅ 已切换到会话: {new_id}")
        return new_id, True
    else:
        print("\n⚠️ 用法: /switch <thread_id>")
        return thread_id, True


async def _cmd_clear(agent: StockAnalysisAgent, thread_id: str) -> tuple[str, bool]:
    await agent.delete_thread(thread_id)
    print(f"\n✅ 已清空会话: {thread_id}")
    return thread_id, True


def _cmd_help(thread_id: str) -> tuple[str, bool]:
    print(COMMAND_HELP)
    return thread_id, True
