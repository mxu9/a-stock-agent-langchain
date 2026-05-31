# src/tools/mcp_adapter.py
import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Type

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field, create_model

from mcp import ClientSession
from mcp.client.sse import sse_client

logger = logging.getLogger(__name__)


class MCPToolAdapter:
    """通过 MCP SDK (SSE) 将远程工具转换为 LangChain StructuredTool"""

    def __init__(self, mcp_config_path: str):
        with open(mcp_config_path, encoding="utf-8") as f:
            self.config = json.load(f)

        # 每个 MCP server 一个 (sse_ctx, session_ctx) 对，用于生命周期管理
        self._contexts: List[tuple] = []
        self.sessions: Dict[str, ClientSession] = {}
        self.tools: List[BaseTool] = []
        self._initialized = False

    async def initialize(self) -> List[BaseTool]:
        """为每个配置的 MCP 服务器建立 SSE 连接，发现并注册所有工具"""
        for server_name, server_cfg in self.config.get("mcpServers", {}).items():
            url = server_cfg.get("url")
            if not url:
                logger.warning("跳过 %s：缺少 url 配置", server_name)
                continue

            logger.info("连接 MCP SSE: %s → %s", server_name, url)

            # ① 建立 SSE 传输通道
            read_stream, write_stream = await self._connect_sse(url)

            # ② 创建会话
            session = await self._create_session(read_stream, write_stream)

            self._contexts.append((server_name, read_stream, write_stream, session))
            self.sessions[server_name] = session

            # ③ 发现工具
            result = await session.list_tools()
            logger.info("%s 发现 %d 个工具", server_name, len(result.tools))

            for tool_meta in result.tools:
                langchain_tool = self._create_tool(session, tool_meta)
                self.tools.append(langchain_tool)

        self._initialized = True
        return self.tools

    async def _connect_sse(self, url: str):
        """建立 SSE 连接，返回 (read_stream, write_stream)"""
        ctx = sse_client(url)
        read_stream, write_stream = await ctx.__aenter__()
        # 把 ctx 存到 write_stream 上，close 时用来退出
        write_stream._sse_ctx = ctx  # type: ignore[attr-defined]
        return read_stream, write_stream

    async def _create_session(
        self, read_stream, write_stream
    ) -> ClientSession:
        session_ctx = ClientSession(read_stream, write_stream)
        session = await session_ctx.__aenter__()
        await session.initialize()
        # 同样把 ctx 存下来用于释放
        session._session_ctx = session_ctx  # type: ignore[attr-defined]
        return session

    # ================================================================
    # 工具转换
    # ================================================================

    def _create_tool(self, session: ClientSession, tool_meta) -> StructuredTool:
        """将 mcp.types.Tool → LangChain StructuredTool"""

        input_schema = self._build_input_schema(tool_meta.inputSchema)

        # 闭包捕获 session 和工具名
        async def tool_func(**kwargs):
            result = await session.call_tool(
                tool_meta.name,
                arguments=kwargs,
            )
            return self._extract_content(result)

        return StructuredTool.from_function(
            coroutine=tool_func,
            name=tool_meta.name,
            description=tool_meta.description or "",
            args_schema=input_schema,
        )

    @staticmethod
    def _extract_content(result) -> str:
        """从 mcp.types.CallToolResult 提取文本"""
        if not hasattr(result, "content"):
            return str(result)

        parts = []
        for item in result.content:
            if hasattr(item, "text"):
                parts.append(item.text)
            elif hasattr(item, "data"):
                parts.append(f"[base64: {len(item.data)} bytes]")
            else:
                parts.append(str(item))
        return "\n".join(parts) if parts else str(result)

    # ================================================================
    # Schema 生成
    # ================================================================

    def _build_input_schema(self, json_schema: Dict) -> Type[BaseModel]:
        properties = json_schema.get("properties", {})
        required = set(json_schema.get("required", []))

        fields = {}
        for name, info in properties.items():
            py_type = self._json_type_to_python(info.get("type"))
            if name in required:
                fields[name] = (
                    py_type,
                    Field(description=info.get("description", "")),
                )
            else:
                fields[name] = (
                    Optional[py_type],
                    Field(default=None, description=info.get("description", "")),
                )

        return create_model("DynamicInput", **fields)

    @staticmethod
    def _json_type_to_python(json_type: Optional[str]) -> Type:
        mapping = {
            "string": str,
            "integer": int,
            "number": float,
            "boolean": bool,
            "array": list,
            "object": dict,
        }
        return mapping.get(json_type, str) if json_type else str

    # ================================================================
    # 查询 / 释放
    # ================================================================

    def get_tools(self) -> List[BaseTool]:
        if not self._initialized:
            raise RuntimeError("工具尚未初始化，请先调用 await adapter.initialize()")
        return self.tools

    async def close(self):
        """释放所有 SSE 连接和会话"""
        for server_name, read, write, session in reversed(self._contexts):
            # 退出 session
            if hasattr(session, "_session_ctx"):
                try:
                    await session._session_ctx.__aexit__(None, None, None)
                except Exception:
                    logger.debug("关闭 session %s 时发生异常", server_name, exc_info=True)
            # 退出 SSE
            if hasattr(write, "_sse_ctx"):
                try:
                    await write._sse_ctx.__aexit__(None, None, None)
                except Exception:
                    logger.debug("关闭 SSE %s 时发生异常", server_name, exc_info=True)

        self._contexts.clear()
        self._initialized = False


# ============================================================
# main — 快速验证
# ============================================================
async def main():
    import os

    config_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "config", "mcp_config.json"
    )
    config_path = os.path.abspath(config_path)

    print(f"📡 连接 MCP SSE 服务器: {config_path}")
    adapter = MCPToolAdapter(config_path)

    try:
        tools = await adapter.initialize()
        print(f"✅ 发现 {len(tools)} 个工具:\n")

        for i, tool in enumerate(tools, 1):
            print(f"{'─' * 50}")
            print(f"[{i}] {tool.name}")
            print(f"    描述: {tool.description}")
            if tool.args_schema:
                schema = tool.args_schema.model_json_schema()
                props = schema.get("properties", {})
                required = schema.get("required", [])
                if props:
                    print(f"    参数:")
                    for name, info in props.items():
                        mark = " (必填)" if name in (required or []) else " (可选)"
                        print(f"      • {name}: {info.get('type', '?')}{mark}")
            print()

        print(f"{'─' * 50}")
        print("✅ 所有工具加载完毕。")

        # ── 健康检查：找到 health_check 工具并调用 ──
        health_tool = None
        for tool in tools:
            if tool.name == "health_check":
                health_tool = tool
                break

        if health_tool is None:
            print("\n⚠️  未找到 health_check 工具，跳过健康检查。")
        else:
            print(f"\n🩺 调用 health_check ...")
            try:
                # StructuredTool 的异步调用入口是 .coroutine
                result = await health_tool.coroutine()
                print(f"✅ MCP 服务器正常: {result}")
            except Exception as e:
                print(f"❌ 健康检查失败: {e}")
    finally:
        await adapter.close()
        print("🔌 已断开连接。")


if __name__ == "__main__":
    asyncio.run(main())
