# src/agents/stock_agent.py
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver

from src.tools.mcp_adapter import MCPToolAdapter
from src.prompts.system_prompt import SYSTEM_PROMPT

class StockAnalysisAgent:
    """A股分析智能体"""
    
    def __init__(self, mcp_config_path: str, model_config: dict = None):
        self.mcp_adapter = MCPToolAdapter(mcp_config_path)
        if model_config is not None:
            self.model_config = model_config
        else:
            from config.settings import LLMSettings
            self.model_config = LLMSettings().to_dict()
        self._agent = None
        self._checkpointer = None
    
    async def initialize(self):
        """异步初始化：加载MCP工具并创建Agent"""
        # 1. 加载MCP工具
        tools = await self.mcp_adapter.initialize()
        print(f"✅ 加载了 {len(tools)} 个MCP工具")

        # 2. 校验必要配置（尽早失败，避免延迟到首次对话才报错）
        if not self.model_config.get("api_key"):
            raise ValueError(
                "未配置 LLM_API_KEY，请在 .env 中设置 LLM_API_KEY=your-key "
                "或在构造时传入 model_config={'api_key': '...'}"
            )

        # 3. 初始化LLM
        llm = ChatOpenAI(
            model=self.model_config.get("model", "deepseek-v4-pro"),
            base_url=self.model_config.get("base_url", "https://api.deepseek.com"),
            api_key=self.model_config.get("api_key"),
            temperature=self.model_config.get("temperature", 0.7),
            streaming=True,  # 开启流式
        )
        
        # 4. 初始化记忆（LangGraph checkpointer，自动持久化对话状态）
        self._checkpointer = MemorySaver()
        
        # 5. 创建Agent
        self._agent = create_agent(
            model=llm,
            tools=tools,
            system_prompt=SYSTEM_PROMPT,
            checkpointer=self._checkpointer,
        )
        
        return self
    
    async def chat(self, user_input: str):
        """与Agent对话，流式返回"""
        async for chunk in self._agent.astream(
            {"messages": [HumanMessage(content=user_input)]}
        ):
            yield chunk
    
    async def chat_with_tool_feedback(self, user_input: str):
        """带工具调用反馈的对话"""
        # 可以监听工具调用过程
        async for event in self._agent.astream_events(
            {"messages": [HumanMessage(content=user_input)]},
            version="v2"
        ):
            if event["event"] == "on_tool_start":
                print(f"🔧 正在调用工具: {event['name']}")
            elif event["event"] == "on_tool_end":
                print(f"✅ 工具执行完成")
            elif event["event"] == "on_chat_model_stream":
                chunk = event["data"]["chunk"]
                # AIMessageChunk → .content 可能是 str 或 list[dict]
                if hasattr(chunk, "content"):
                    content = chunk.content
                    if isinstance(content, str):
                        yield content
                    elif isinstance(content, list):
                        # 多模态内容，只提取 text 部分
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                yield item.get("text", "")
                    else:
                        yield str(content)
                elif isinstance(chunk, str):
                    yield chunk
    
    def get_checkpointer(self):
        """返回 checkpointer，供外部按 thread_id 查询历史"""
        return self._checkpointer

    async def close(self):
        """释放 MCP SSE 连接和所有资源"""
        await self.mcp_adapter.close()


# ============================================================
# main — 集成测试
# ============================================================
async def main():
    import os

    config_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "config", "mcp_config.json"
    )
    config_path = os.path.abspath(config_path)

    print("=" * 60)
    print("🚀 创建 StockAnalysisAgent")
    print("=" * 60)

    agent = StockAnalysisAgent(config_path)
    await agent.initialize()

    thread = {"configurable": {"thread_id": "integration-test"}}

    try:
        # ── 多轮对话 ──
        questions = [
            "你的能力是什么",
            "服务器是否正常连接",
            "收集 江苏金租 最近一周的事件，并分析其影响"]

        for i, question in enumerate(questions, 1):
            print(f"\n{'─' * 60}")
            print(f"💬 Chat {i}: {question}")
            print(f"{'─' * 60}")

            async for event in agent._agent.astream_events(
                {"messages": [HumanMessage(content=question)]},
                config=thread,
                version="v2",
            ):
                if event["event"] == "on_tool_start":
                    print(f"\n  🔧 调用工具: {event['name']}", flush=True)
                elif event["event"] == "on_tool_end":
                    print(f"  ✅ 工具返回", flush=True)
                elif event["event"] == "on_chat_model_stream":
                    chunk = event["data"]["chunk"]
                    if hasattr(chunk, "content"):
                        content = chunk.content
                        if isinstance(content, str):
                            print(content, end="", flush=True)
            print()  # 换行

        # ── 历史记录 ──
        print(f"\n{'=' * 60}")
        print("📜 对话历史")
        print("=" * 60)

        state = await agent._agent.aget_state(thread)
        if state and state.values:
            messages = state.values.get("messages", [])
            if messages:
                for msg in messages:
                    role_map = {"human": "👤 用户", "ai": "🤖 助手", "tool": "🔧 工具"}
                    role = role_map.get(msg.type, f"❓ {msg.type}")
                    content = msg.content
                    if isinstance(content, str) and len(content) > 150:
                        content = content[:150] + "..."
                    print(f"  {role}: {content}")
            else:
                print("  (无历史消息)")
        else:
            print("  (无可用状态)")
    finally:
        # ── 销毁 ──
        print(f"\n{'=' * 60}")
        print("🔌 销毁 Agent")
        print("=" * 60)
        await agent.close()
        print("✅ 测试完成")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())