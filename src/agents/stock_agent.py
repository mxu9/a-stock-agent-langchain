# src/agents/stock_agent.py
from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langchain.memory import ConversationBufferMemory
from langchain.callbacks import StdOutCallbackHandler

from src.tools.mcp_adapter import MCPToolAdapter
from src.prompts.system_prompt import SYSTEM_PROMPT

class StockAnalysisAgent:
    """A股分析智能体"""
    
    def __init__(self, mcp_config_path: str, model_config: dict):
        self.mcp_adapter = MCPToolAdapter(mcp_config_path)
        self.model_config = model_config
        self._agent = None
        self._memory = None
    
    async def initialize(self):
        """异步初始化：加载MCP工具并创建Agent"""
        # 1. 加载MCP工具
        tools = await self.mcp_adapter.initialize()
        print(f"✅ 加载了 {len(tools)} 个MCP工具")
        
        # 2. 初始化LLM
        llm = ChatOpenAI(
            model=self.model_config.get("model", "deepseek-v4-pro"),
            base_url=self.model_config.get("base_url", "https://api.deepseek.com"),
            api_key=self.model_config.get("api_key"),
            temperature=self.model_config.get("temperature", 0.7),
            streaming=True,  # 开启流式
        )
        
        # 3. 初始化记忆
        self._memory = ConversationBufferMemory(
            memory_key="chat_history",
            return_messages=True,
            max_token_limit=4000,  # 控制上下文长度
        )
        
        # 4. 创建Agent
        self._agent = create_agent(
            model=llm,
            tools=tools,
            system_prompt=SYSTEM_PROMPT,
            memory=self._memory,
            # 中间件配置（后续扩展）
            # middleware=[LoggingMiddleware()],
        )
        
        return self
    
    async def chat(self, user_input: str):
        """与Agent对话，流式返回"""
        async for chunk in self._agent.astream(
            {"messages": [{"role": "user", "content": user_input}]}
        ):
            yield chunk
    
    async def chat_with_tool_feedback(self, user_input: str):
        """带工具调用反馈的对话"""
        # 可以监听工具调用过程
        async for event in self._agent.astream_events(
            {"messages": [{"role": "user", "content": user_input}]},
            version="v2"
        ):
            if event["event"] == "on_tool_start":
                print(f"🔧 正在调用工具: {event['name']}")
            elif event["event"] == "on_tool_end":
                print(f"✅ 工具执行完成")
            elif event["event"] == "on_chat_model_stream":
                # 流式输出LLM生成的文本
                yield event["data"]["chunk"].content
    
    def get_conversation_history(self):
        """获取对话历史"""
        return self._memory.load_memory_variables({})