# tests/test_mcp_adapter.py
import json
from typing import Optional

import pytest
from pydantic import BaseModel

from src.tools.mcp_adapter import MCPToolAdapter


# ============================================================
# 辅助 mock 类
# ============================================================

class MockTool:
    """模拟 mcp.types.Tool"""
    def __init__(self, name: str, description: str, inputSchema: dict):
        self.name = name
        self.description = description
        self.inputSchema = inputSchema


class MockListToolsResult:
    """模拟 mcp.types.ListToolsResult"""
    def __init__(self, tools: list):
        self.tools = tools


class MockTextContent:
    """模拟 mcp.types.TextContent"""
    def __init__(self, text: str):
        self.text = text


class MockCallToolResult:
    """模拟 mcp.types.CallToolResult"""
    def __init__(self, content: list):
        self.content = content


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def sample_config():
    return {
        "mcpServers": {
            "test-service": {
                "transport": "sse",
                "url": "http://localhost:9999/sse",
            }
        }
    }


@pytest.fixture
def config_file(sample_config, tmp_path):
    path = tmp_path / "mcp_config.json"
    path.write_text(json.dumps(sample_config), encoding="utf-8")
    return str(path)


@pytest.fixture
def adapter(config_file):
    return MCPToolAdapter(config_file)


# ============================================================
# __init__
# ============================================================

class TestInit:
    def test_load_valid_config(self, config_file, sample_config):
        adapter = MCPToolAdapter(config_file)
        assert adapter.config == sample_config

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            MCPToolAdapter("/nonexistent/path/config.json")

    def test_invalid_json(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("not json{{{", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            MCPToolAdapter(str(bad))

    def test_initial_state(self, adapter):
        assert adapter.sessions == {}
        assert adapter.tools == []
        assert adapter._initialized is False


# ============================================================
# _json_type_to_python
# ============================================================

class TestJsonTypeToPython:
    @pytest.mark.parametrize("json_type, expected", [
        ("string", str), ("integer", int), ("number", float),
        ("boolean", bool), ("array", list), ("object", dict),
    ])
    def test_known_types(self, adapter, json_type, expected):
        assert MCPToolAdapter._json_type_to_python(json_type) is expected

    def test_unknown_type_defaults_to_str(self, adapter):
        assert MCPToolAdapter._json_type_to_python("unknown") is str

    def test_none_defaults_to_str(self, adapter):
        assert MCPToolAdapter._json_type_to_python(None) is str


# ============================================================
# _build_input_schema
# ============================================================

class TestBuildInputSchema:
    def test_required_fields(self, adapter):
        schema = {
            "properties": {"symbol": {"type": "string", "description": "股票代码"}},
            "required": ["symbol"],
        }
        model = adapter._build_input_schema(schema)
        with pytest.raises(Exception):
            model()
        instance = model(symbol="000001")
        assert instance.symbol == "000001"

    def test_optional_fields(self, adapter):
        schema = {
            "properties": {"limit": {"type": "integer", "description": "返回条数"}},
            "required": [],
        }
        model = adapter._build_input_schema(schema)
        instance = model()
        assert instance.limit is None
        instance = model(limit=10)
        assert instance.limit == 10

    def test_mixed_required_and_optional(self, adapter):
        schema = {
            "properties": {
                "symbol": {"type": "string", "description": "股票代码"},
                "days": {"type": "integer", "description": "查询天数"},
            },
            "required": ["symbol"],
        }
        model = adapter._build_input_schema(schema)
        instance = model(symbol="000001")
        assert instance.symbol == "000001"
        assert instance.days is None

    def test_empty_schema(self, adapter):
        model = adapter._build_input_schema({})
        instance = model()
        assert instance is not None

    def test_model_is_pydantic_base_model(self, adapter):
        schema = {
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        model = adapter._build_input_schema(schema)
        assert issubclass(model, BaseModel)


# ============================================================
# get_tools 状态保护
# ============================================================

class TestGetTools:
    def test_raises_when_not_initialized(self, adapter):
        with pytest.raises(RuntimeError, match="尚未初始化"):
            adapter.get_tools()


# ============================================================
# initialize — mock sse_client + ClientSession
# ============================================================

class TestInitialize:
    async def test_initialize_loads_tools(self, adapter, mocker):
        tool_a = MockTool("get_price", "获取股票价格", {
            "properties": {"symbol": {"type": "string", "description": "代码"}},
            "required": ["symbol"],
        })
        tool_b = MockTool("get_news", "获取新闻", {
            "properties": {"keyword": {"type": "string", "description": "关键词"}},
            "required": [],
        })

        # mock session
        mock_session = mocker.MagicMock()
        mock_session.list_tools = mocker.AsyncMock(
            return_value=MockListToolsResult([tool_a, tool_b])
        )
        mock_session.initialize = mocker.AsyncMock()

        # mock session context manager
        session_ctx = mocker.MagicMock()
        session_ctx.__aenter__ = mocker.AsyncMock(return_value=mock_session)
        session_ctx.__aexit__ = mocker.AsyncMock()
        mock_session._session_ctx = session_ctx

        # mock sse_client → (read, write) 上下文
        mock_write = mocker.MagicMock()
        mock_write._sse_ctx = mocker.MagicMock()
        mock_write._sse_ctx.__aenter__ = mocker.AsyncMock(return_value=(mocker.MagicMock(), mock_write))
        mock_write._sse_ctx.__aexit__ = mocker.AsyncMock()

        mocker.patch(
            "src.tools.mcp_adapter.sse_client",
            return_value=mock_write._sse_ctx,
        )
        mocker.patch(
            "src.tools.mcp_adapter.ClientSession",
            return_value=session_ctx,
        )

        result = await adapter.initialize()

        assert len(result) == 2
        assert adapter._initialized is True
        assert result[0].name == "get_price"
        assert result[0].description == "获取股票价格"
        assert result[1].name == "get_news"
        assert "test-service" in adapter.sessions

    async def test_initialize_with_no_tools(self, adapter, mocker):
        mock_session = mocker.MagicMock()
        mock_session.list_tools = mocker.AsyncMock(
            return_value=MockListToolsResult([])
        )
        mock_session.initialize = mocker.AsyncMock()

        session_ctx = mocker.MagicMock()
        session_ctx.__aenter__ = mocker.AsyncMock(return_value=mock_session)
        session_ctx.__aexit__ = mocker.AsyncMock()
        mock_session._session_ctx = session_ctx

        mock_write = mocker.MagicMock()
        mock_write._sse_ctx = mocker.MagicMock()
        mock_write._sse_ctx.__aenter__ = mocker.AsyncMock(return_value=(mocker.MagicMock(), mock_write))
        mock_write._sse_ctx.__aexit__ = mocker.AsyncMock()

        mocker.patch("src.tools.mcp_adapter.sse_client", return_value=mock_write._sse_ctx)
        mocker.patch("src.tools.mcp_adapter.ClientSession", return_value=session_ctx)

        result = await adapter.initialize()
        assert result == []


# ============================================================
# _create_tool — session.call_tool + 内容提取
# ============================================================

class TestCreateTool:
    @pytest.fixture
    def tool_meta(self):
        return MockTool("get_price", "获取股票价格", {
            "properties": {"symbol": {"type": "string", "description": "股票代码"}},
            "required": ["symbol"],
        })

    async def test_extracts_text_content(self, adapter, mocker, tool_meta):
        mock_session = mocker.MagicMock()
        mock_session.call_tool = mocker.AsyncMock(
            return_value=MockCallToolResult([MockTextContent("股价: 12.58 元")])
        )

        tool = adapter._create_tool(mock_session, tool_meta)
        result = await tool.coroutine(symbol="000001")

        mock_session.call_tool.assert_awaited_once_with(
            "get_price", arguments={"symbol": "000001"}
        )
        assert result == "股价: 12.58 元"

    async def test_multi_part_content(self, adapter, mocker, tool_meta):
        mock_session = mocker.MagicMock()
        mock_session.call_tool = mocker.AsyncMock(
            return_value=MockCallToolResult([
                MockTextContent("第一行"),
                MockTextContent("第二行"),
            ])
        )

        tool = adapter._create_tool(mock_session, tool_meta)
        result = await tool.coroutine(symbol="000002")
        assert result == "第一行\n第二行"

    async def test_fallback_no_content_attr(self, adapter, mocker, tool_meta):
        mock_session = mocker.MagicMock()
        mock_session.call_tool = mocker.AsyncMock(return_value="raw string")

        tool = adapter._create_tool(mock_session, tool_meta)
        result = await tool.coroutine(symbol="000003")
        assert result == "raw string"


# ============================================================
# close — 释放 SSE + Session 上下文
# ============================================================

class TestClose:
    async def test_close_releases_contexts(self, adapter, mocker):
        sse_ctx = mocker.MagicMock()
        sse_ctx.__aexit__ = mocker.AsyncMock()
        session_ctx = mocker.MagicMock()
        session_ctx.__aexit__ = mocker.AsyncMock()

        mock_write = mocker.MagicMock()
        mock_write._sse_ctx = sse_ctx
        mock_session = mocker.MagicMock()
        mock_session._session_ctx = session_ctx

        adapter._contexts.append(("srv", mocker.MagicMock(), mock_write, mock_session))
        adapter._initialized = True

        await adapter.close()

        session_ctx.__aexit__.assert_called()
        sse_ctx.__aexit__.assert_called()
        assert adapter._initialized is False

    async def test_close_when_no_contexts(self, adapter):
        adapter._contexts = []
        await adapter.close()  # 不应报错
