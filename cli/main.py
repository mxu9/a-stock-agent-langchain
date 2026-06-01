#!/usr/bin/env python3
"""
A股分析大师 - CLI 版本
支持流式输出、多轮对话、会话管理
"""

import asyncio
import sys
from pathlib import Path
from typing import Optional

from src.agents.stock_agent import StockAnalysisAgent
from cli.commands import handle_command, TIPS


class StockAgentCLI:
    """A股分析大师 CLI"""

    def __init__(self):
        self.agent: Optional[StockAnalysisAgent] = None
        self.current_thread_id = "default"
        self.running = True

    async def initialize(self):
        """初始化 Agent"""
        print("\n" + "=" * 60)
        print("📈  A股分析大师 v1.0")
        print("=" * 60)
        print("\n🚀 正在初始化 Agent...")

        config_path = Path(__file__).parent.parent / "config" / "mcp_config.json"
        self.agent = StockAnalysisAgent(str(config_path))
        await self.agent.initialize()

        print("✅ Agent 初始化完成")
        print(TIPS)
        print("=" * 60)

    async def run(self):
        """运行主循环"""
        await self.initialize()

        try:
            while self.running:
                try:
                    user_input = await self._get_user_input()
                    if not user_input:
                        continue
                    if user_input.startswith("/"):
                        await self._handle_command(user_input)
                        continue
                    await self._process_message(user_input)
                except KeyboardInterrupt:
                    print("\n\n👋 再见！")
                    break
                except Exception as e:
                    print(f"\n❌ 错误: {e}")
        finally:
            await self.cleanup()

    async def _get_user_input(self) -> str:
        """获取用户输入（空行结束多行模式）"""
        print(f"\n{'─' * 40}")
        print(f"💬 [{self.current_thread_id}] > ", end="", flush=True)

        lines = []
        while True:
            try:
                line = await asyncio.get_event_loop().run_in_executor(
                    None, sys.stdin.readline
                )
                if not line:
                    return ""
                line = line.rstrip("\n")
                if line == "":
                    if lines:
                        break
                    else:
                        continue
                if line == "." and not lines:
                    return ""
                lines.append(line)
            except KeyboardInterrupt:
                raise

        if not lines:
            return ""
        if len(lines) == 1:
            return lines[0]
        return "\n".join(lines)

    async def _process_message(self, user_input: str):
        """处理用户消息并流式输出"""
        print(f"\n🤖 Assistant: ", end="", flush=True)

        try:
            async for chunk in self.agent.chat_with_tool_feedback(
                user_input, thread_id=self.current_thread_id
            ):
                print(chunk, end="", flush=True)
            print()
        except Exception as e:
            print(f"\n❌ 处理失败: {e}")

    async def _handle_command(self, command: str):
        """委托给 commands 模块处理"""
        self.current_thread_id, self.running = await handle_command(
            command, self.agent, self.current_thread_id
        )

    async def cleanup(self):
        """清理资源"""
        if self.agent:
            await self.agent.close()
            print("\n✅ 资源已释放")


def main():
    """入口函数"""
    async def run():
        cli = StockAgentCLI()
        await cli.run()

    try:
        asyncio.run(run())
    except Exception as e:
        print(f"\n❌ 程序异常: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
