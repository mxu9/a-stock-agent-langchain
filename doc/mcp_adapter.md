# MCPToolAdapter 设计文档

## 1. 概述

`MCPToolAdapter` 是一道**桥梁**：它把 MCP（Model Context Protocol）远程服务器暴露的工具，动态翻译为 LangChain 可识别的 `StructuredTool` 对象，从而让 LangChain Agent 能够调用外部 MCP 工具。

### 核心价值

```
mcp_config.json  ──→  MCPToolAdapter  ──→  List[StructuredTool]  ──→  LangChain Agent
```

- **解耦协议**：Agent 不知道 MCP 协议的存在，只看到标准的 LangChain Tool
- **动态发现**：无需手动声明工具，服务器有什么工具就注册什么
- **多服务器**：支持同时连接多个 MCP 服务器，每个服务器的工具统一管理

---

## 2. 架构概览

```
                        MCPToolAdapter
┌─────────────┐         ┌──────────────────────────────┐
│ mcp_config  │──加载──→│  self.config (dict)           │
│ .json       │         │                              │
└─────────────┘         │  initialize()                 │
                        │    ├─ sse_client(url)         │──── SSE 传输 ──→ MCP Server A
                        │    │    └─ ClientSession      │
                        │    │         └─ list_tools()  │←── Tool[] ──
                        │    │         └─ call_tool()   │──→ result
                        │    │                         │
                        │    ├─ sse_client(url)         │──── SSE 传输 ──→ MCP Server B
                        │    │    └─ ...                │
                        │    │                         │
                        │    └─ _create_tool()          │
                        │         ├─ _build_input_schema│
                        │         └─ StructuredTool     │
                        │                              │
                        │  get_tools() ────────────────→ List[BaseTool]
                        │  close() ──→ 释放 SSE + Session
                        └──────────────────────────────┘
```

### 外部依赖

| 依赖 | 角色 |
|------|------|
| `mcp.client.sse.sse_client` | 建立 SSE 长连接，返回双向流 |
| `mcp.ClientSession` | JSON-RPC 会话，`list_tools()` / `call_tool()` |
| `mcp.types.Tool` | 工具元数据：`.name` / `.description` / `.inputSchema` |
| `mcp.types.CallToolResult` | 调用返回值：`.content` (list of TextContent / ImageContent) |
| `langchain_core.tools.StructuredTool` | LangChain 工具包装 |
| `pydantic.create_model` | 动态生成参数校验模型 |

---

## 3. 类设计

### 3.1 字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `config` | `dict` | 从 JSON 文件加载的原始配置 |
| `_contexts` | `List[tuple]` | 上下文栈：`(server_name, read_stream, write_stream, session)` |
| `sessions` | `Dict[str, ClientSession]` | server_name → 活跃的 MCP 会话 |
| `tools` | `List[BaseTool]` | 转换后的 LangChain 工具列表 |
| `_initialized` | `bool` | 状态锁，防止 `get_tools()` 在初始化前被调用 |

### 3.2 生命周期

```
    构造                异步初始化              同步获取             清理
    ┌──┐      ┌───────────┐      ┌───────────┐      ┌──────┐
    │  │  →   │           │  →   │           │  →   │      │
    │__init__│  │initialize│      │ get_tools │      │close │
    └──┘      └───────────┘      └───────────┘      └──────┘

  加载配置     SSE 连接              返回给        释放 SSE +
             发现工具              LangChain       Session 上下文
             动态转换
```

---

## 4. 公共 API

### 4.1 `__init__(mcp_config_path: str)`

**功能**：加载配置文件，初始化内部状态。

**参数**：
- `mcp_config_path` — `mcp_config.json` 的路径

**行为**：
- 读取并解析 JSON 配置文件到 `self.config`
- 所有连接相关字段置空，等待 `initialize()` 调用

**异常**：
- `FileNotFoundError` — 配置文件不存在
- `json.JSONDecodeError` — 配置不是合法 JSON

---

### 4.2 `async initialize() -> List[BaseTool]`

**功能**：连接所有 MCP 服务器，发现工具，转换为 LangChain Tool。这是整个适配器的核心入口。

**工作流程**：

```
for each server in mcpServers:
    │
    ├─ ① _connect_sse(url)
    │     sse_client(url)  →  (read_stream, write_stream)
    │     通过 SSE 协议建立到 MCP 服务器的长连接
    │
    ├─ ② _create_session(read, write)
    │     ClientSession(read, write)  →  session.initialize()
    │     在 SSE 通道上建立 JSON-RPC 会话
    │     session.initialize() 完成 MCP 协议的握手（能力协商）
    │
    ├─ ③ session.list_tools()
    │     返回 ListToolsResult，其中 .tools 是 mcp.types.Tool 列表
    │     每个 Tool 包含:
    │       - name: str          → "get_stock_price"
    │       - description: str   → "获取股票实时价格"
    │       - inputSchema: dict  → JSON Schema 格式的参数定义
    │
    └─ ④ _create_tool(session, tool_meta)
          每个 mcp.types.Tool → 一个 LangChain StructuredTool
```

**返回值**：`List[BaseTool]`，可直接传给 `create_react_agent(tools=...)` 等 LangChain API。

**注意**：此方法是**幂等的**吗？不是——每次调用会新增连接，不会自动去重。预期只调用一次。

---

### 4.3 `get_tools() -> List[BaseTool]`

**功能**：返回已初始化的工具列表。

**防卫性设计**：如果 `initialize()` 尚未被调用，抛出 `RuntimeError("工具尚未初始化，请先调用 await adapter.initialize()")`。这样做是为了避免静默返回空列表导致难以调试的问题。

---

### 4.4 `async close()`

**功能**：释放所有 SSE 连接和 MCP 会话。

**释放顺序**（关键设计决策）：

```
for context in reversed(self._contexts):    # ← 逆序释放
    await session._session_ctx.__aexit__()  # 先关会话
    await write._sse_ctx.__aexit__()        # 再关传输
```

**为什么逆序**：后创建的 Session 依赖先创建的 SSE 连接。逆序释放保证 Session 在其依赖的 SSE 连接之前关闭，避免 "连接已关闭但 Session 仍尝试通信" 的竞态。

**为什么用 `try/except`**：释放是 best-effort——一个服务器的释放失败不应阻止其他服务器的清理。

---

## 5. 内部 API

### 5.1 `_connect_sse(url: str) -> (read_stream, write_stream)`

**功能**：建立到 MCP 服务器的 SSE 连接。

**为什么把 `_sse_ctx` 挂到 `write_stream` 上**：

```python
ctx = sse_client(url)
read_stream, write_stream = await ctx.__aenter__()
write_stream._sse_ctx = ctx
```

`ctx` 是异步上下文管理器，必须保持引用以便 `close()` 时调用 `__aexit__`。但 `initialize()` 把 stream 和 ctx 都放入 `_contexts`，这是一种**上下文栈**模式——通过附加属性让 `close()` 能找到对应的退出方法。

### 5.2 `_create_session(read_stream, write_stream) -> ClientSession`

**功能**：在 SSE 通道之上创建 MCP JSON-RPC 会话。

**`session.initialize()` 做了什么**：这是 MCP 协议规定的握手步骤——客户端向服务器发送 `initialize` 请求，声明自己的能力和协议版本，服务器返回支持的能力列表。握手完成后，`list_tools()` 和 `call_tool()` 才可用。

### 5.3 `_create_tool(session, tool_meta) -> StructuredTool`

**功能**：将一个 MCP 工具转换为 LangChain `StructuredTool`。

**核心设计——闭包**：

```python
async def tool_func(**kwargs):
    result = await session.call_tool(tool_meta.name, arguments=kwargs)
    return self._extract_content(result)
```

`tool_func` 是一个闭包，捕获了两个变量：
- `session` — 该工具所属的 MCP 会话（用于实际调用）
- `tool_meta.name` — MCP 工具名

LangChain 调用 `tool_func(symbol="000001")` 时，完全不感知底层是 MCP 远程调用。

### 5.4 `_extract_content(result) -> str`

**功能**：从 `CallToolResult` 中提取文本，返回 LangChain 友好的纯字符串。

**`result.content` 的结构**：

```
CallToolResult
  └─ content: list[TextContent | ImageContent | EmbeddedResource]
        ├─ TextContent.text    → "股价: 12.58 元"
        ├─ ImageContent.data   → base64 字节
        └─ EmbeddedResource    → 嵌入资源
```

**处理逻辑**：
1. 有 `.text` → 提取文本
2. 有 `.data` → 标注 `[base64: N bytes]`（LangChain Agent 无法处理图片）
3. 其他 → `str(item)` 兜底
4. 多个 content → `\n` 拼接

### 5.5 `_build_input_schema(json_schema: dict) -> Type[BaseModel]`

**功能**：把 MCP 工具的 JSON Schema 参数定义，动态生成为 Pydantic 模型类。

**为什么需要 Pydantic 模型**：LangChain 的 `StructuredTool` 要求 `args_schema` 必须是 `BaseModel` 子类。LLM 通过读取模型的 JSON Schema 来生成函数调用参数，Pydantic 负责在 Python 侧做参数校验。

**输入 → 输出示例**：

```
输入 (json_schema):
{
  "properties": {
    "symbol": {"type": "string", "description": "股票代码"},
    "days":   {"type": "integer", "description": "查询天数"}
  },
  "required": ["symbol"]
}

输出 (运行时动态等价于):
class DynamicInput(BaseModel):
    symbol: str = Field(description="股票代码")
    days: Optional[int] = Field(default=None, description="查询天数")
```

### 5.6 `_json_type_to_python(json_type) -> Type`

**功能**：JSON Schema 类型名 → Python 类型的一对一映射。

| JSON Schema | Python |
|-------------|--------|
| `"string"` | `str` |
| `"integer"` | `int` |
| `"number"` | `float` |
| `"boolean"` | `bool` |
| `"array"` | `list` |
| `"object"` | `dict` |
| 未知 / `None` | `str` (回退) |

---

## 6. 配置文件格式

```json
{
  "mcpServers": {
    "<服务器名称>": {
      "transport": "sse",
      "url": "http://<host>:<port>/<path>"
    }
  }
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `mcpServers` | 是 | 顶层键，固定名称 |
| `<服务器名称>` | 是 | 任意字符串，用于日志标识 |
| `transport` | 否（当前忽略） | 保留字段 |
| `url` | 是 | MCP 服务器的 SSE 端点，如 `http://localhost:8000/sse` |

---

## 7. 关键设计决策

### 7.1 为什么用闭包而非类方法

`_create_tool` 生成的 `tool_func` 是闭包，每个工具独立捕获自己的 `session` 和 `tool_meta.name`。这样做的好处：

- 多服务器场景下，不同工具的调用路由到正确的 session
- LangChain 调用时不需要传递 session 引用
- 符合 `StructuredTool.from_function(coroutine=...)` 的接口约定

### 7.2 为什么用 `create_model` 而非拼 `__annotations__`

Pydantic v2 中，`Field()` 对象不能放在 `__annotations__` 字典里，必须通过 `create_model(**fields)` 传入。这是官方推荐的动态模型创建方式，内部会自动分离 annotations 和 pydantic_fields。

### 7.3 为什么 SSE 而非其他传输

- **简单**：标准 HTTP + SSE，无需 WebSocket 或 stdio
- **可靠**：SSE 自动重连，单向推送，适合工具调用的响应模式
- **MCP SDK 原生支持**：`mcp.client.sse.sse_client` 开箱即用

### 7.4 为什么 `_initialized` 是显式状态锁

`get_tools()` 被同步调用，`initialize()` 是异步的。如果用"判断 tools 是否为空列表"来判断是否已初始化，当服务器真的没有工具时，会返回空列表——和"未初始化"无法区分。显式的 `_initialized` 标志消除了这种歧义。

### 7.5 上下文栈管理

`_contexts` 是一个栈，存储 `(server_name, read_stream, write_stream, session)` 四元组。`close()` 时逆序遍历，先退出 Session 上下文（停止 JSON-RPC 通信），再退出 SSE 上下文（关闭 HTTP 连接）。

---

## 8. 使用示例

### 基本用法

```python
from src.tools.mcp_adapter import MCPToolAdapter

adapter = MCPToolAdapter("config/mcp_config.json")
tools = await adapter.initialize()

# 传入 LangChain Agent
from langgraph.prebuilt import create_react_agent
agent = create_react_agent(llm, tools)

# 使用完毕后释放
await adapter.close()
```

### 搭配 try/finally

```python
adapter = MCPToolAdapter("config/mcp_config.json")
try:
    tools = await adapter.initialize()
    print(f"加载了 {len(tools)} 个工具")
    # ... 使用 tools ...
finally:
    await adapter.close()
```

### 命令行验证

```bash
python -m src.tools.mcp_adapter
```

输出：
```
📡 连接 MCP SSE 服务器: /path/to/config/mcp_config.json
✅ 发现 15 个工具:

──────────────────────────────────────────────────
[1] get_market_data
    描述: 获取A股上市公司实时行情与估值数据
    参数:
      • symbol: string (必填)
      • fields: string (可选)
──────────────────────────────────────────────────
...
──────────────────────────────────────────────────
✅ 所有工具加载完毕。
🔌 已断开连接。
```

---

## 9. 错误处理策略

| 场景 | 策略 |
|------|------|
| 配置文件不存在 | 抛出 `FileNotFoundError`，不做降级 |
| 配置文件非合法 JSON | 抛出 `json.JSONDecodeError`，不做降级 |
| 服务器缺少 `url` 字段 | 打印 WARNING 日志，跳过该服务器 |
| SSE 连接失败 | 抛出原始异常，不捕获——让调用方决定如何处理 |
| `get_tools()` 在初始化前被调用 | 抛出 `RuntimeError` 明确告知调用顺序错误 |
| `close()` 中单个服务器释放失败 | 捕获异常，打印 DEBUG 日志，继续释放其余服务器 |
| `sse_client` 或 `session` 上未挂载 `_ctx` | `hasattr` 检查，安全跳过 |
