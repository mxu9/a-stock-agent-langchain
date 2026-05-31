# StockAnalysisAgent 设计文档

## 1. 概述

`StockAnalysisAgent` 是 A 股分析智能体的顶层封装。它组合了三个子系统：

```
┌──────────────────────────────────────────────────┐
│              StockAnalysisAgent                  │
│                                                  │
│  ┌────────────┐  ┌──────────┐  ┌─────────────┐  │
│  │ MCP 工具层  │  │ LLM 层   │  │ 记忆层       │  │
│  │            │  │          │  │             │  │
│  │ MCPTool   │  │ChatOpenAI│  │ MemorySaver │  │
│  │ Adapter   │  │          │  │             │  │
│  └─────┬──────┘  └────┬─────┘  └──────┬──────┘  │
│        │              │               │          │
│        └──────────────┼───────────────┘          │
│                       │                          │
│               create_agent()                     │
│               (LangGraph)                        │
│                       │                          │
│               ┌───────▼───────┐                  │
│               │ CompiledState │                  │
│               │ Graph         │                  │
│               └───────────────┘                  │
└──────────────────────────────────────────────────┘
```

核心职责：**构造 → 初始化 → 对话 → 销毁** 的完整生命周期管理。

---

## 2. 外部依赖

| 依赖 | 角色 |
|------|------|
| `langchain.agents.create_agent` | LangGraph Agent 工厂，返回 `CompiledStateGraph` |
| `langchain_openai.ChatOpenAI` | LLM 调用（兼容 OpenAI API 协议的任意模型） |
| `langgraph.checkpoint.memory.MemorySaver` | 对话状态持久化（checkpointer） |
| `langchain_core.messages.HumanMessage` | 用户消息封装 |
| `src.tools.mcp_adapter.MCPToolAdapter` | MCP 工具发现与转换 |
| `src.prompts.system_prompt.SYSTEM_PROMPT` | Agent 系统提示词 |
| `config.settings.LLMSettings` | 从 `.env` 读取 LLM 配置 |

---

## 3. 类设计

### 3.1 字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `mcp_adapter` | `MCPToolAdapter` | 管理 MCP SSE 连接和工具发现 |
| `model_config` | `dict` | LLM 配置：`model` / `base_url` / `api_key` / `temperature` |
| `_agent` | `CompiledStateGraph` | LangGraph 编译后的 Agent 执行图 |
| `_checkpointer` | `MemorySaver` | 对话状态快照，基于 thread_id 管理多轮记忆 |

### 3.2 生命周期

```
   构造             异步初始化              对话循环              清理
   ┌──┐      ┌──────────────┐      ┌──────────────┐      ┌──────┐
   │  │  →   │              │  →   │              │  →   │      │
   │__init__│  │ initialize  │      │ chat /       │      │close │
   └──┘      │              │      │ chat_with_   │      └──────┘
             └──────────────┘      │ tool_feedback│
  加载配置    ① 加载 MCP 工具        └──────────────┘      释放 SSE
  延迟初始化   ② 校验 api_key        流式返回 AI 回复       清理 memory
             ③ 创建 LLM
             ④ 创建 MemorySaver
             ⑤ 编译 Agent Graph
```

---

## 4. 公共 API

### 4.1 `__init__(mcp_config_path, model_config=None)`

**功能**：构造 Agent，加载配置但不建立任何连接（延迟初始化）。

**参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `mcp_config_path` | `str` | 是 | `mcp_config.json` 路径 |
| `model_config` | `dict` | 否 | 手动指定 LLM 配置，不传则自动从 `.env` 读取 |

**model_config 结构**：

```python
{
    "model":       "deepseek-v4-pro",           # 默认值
    "base_url":    "https://api.deepseek.com",  # 默认值
    "api_key":     "sk-xxx",                    # 无默认值
    "temperature": 0.7,                         # 默认值
}
```

**三种配置方式**：

```python
# 方式 ①：零配置，从 .env 读取
agent = StockAnalysisAgent("config/mcp_config.json")

# 方式 ②：代码传入，覆盖 .env
agent = StockAnalysisAgent("config/mcp_config.json", {
    "model": "gpt-4",
    "api_key": "sk-custom",
})

# 方式 ③：.env + 环境变量（LLMSettings 自动加载）
# export LLM_MODEL=deepseek-chat
```

**配置优先级**：代码传入 > 环境变量 > `.env` 文件 > 默认值

---

### 4.2 `async initialize() -> self`

**功能**：建立 MCP 连接、校验配置、创建 LLM 和 Agent。异步，必须在使用前调用。

**执行步骤**：

```
① 加载 MCP 工具     →  MCPToolAdapter.initialize()
    ├─ SSE 连接 MCP 服务器
    ├─ 发现远程工具
    └─ 转换为 LangChain StructuredTool

② 校验 api_key      →  if empty → ValueError
    尽早失败策略：不在第一次对话时才报错

③ 创建 LLM           →  ChatOpenAI(model, base_url, api_key, temperature, streaming=True)

④ 创建 MemorySaver   →  内存版 checkpointer

⑤ 编译 Agent Graph   →  create_agent(model, tools, system_prompt, checkpointer)
    └─ 返回 CompiledStateGraph
```

**返回值**：`self`，支持链式调用：`agent = await StockAnalysisAgent(...).initialize()`

**异常**：
- `ValueError` — `api_key` 未配置
- MCP 连接失败 → 由 `MCPToolAdapter.initialize()` 向上抛出

---

### 4.3 `async chat(user_input: str) -> AsyncGenerator`

**功能**：发送用户消息，流式返回 Agent 的响应。

**输入**：`user_input` — 用户自然语言输入

**输出**：`AsyncGenerator`，每次 yield LangGraph state 快照（dict）。调用方可以遍历获取，但不做格式转换——原始 state 包含 `messages` 列表。

**注意**：当前 `chat()` 不传 `config` 参数，因此 checkpointer 不会跨轮关联。如需多轮记忆，建议调用方直接使用 `agent._agent.astream(..., config={"configurable": {"thread_id": "..."}})`。

---

### 4.4 `async chat_with_tool_feedback(user_input: str) -> AsyncGenerator`

**功能**：与 `chat()` 相同，但会 yield 工具调用的实时反馈文本。

**事件处理**：

| 事件 | 行为 |
|------|------|
| `on_tool_start` | `print("🔧 正在调用工具: {name}")` |
| `on_tool_end` | `print("✅ 工具执行完成")` |
| `on_chat_model_stream` | yield 提取后的文本 |

**内容提取策略**（三层安全）：

```python
chunk = event["data"]["chunk"]

① hasattr(chunk, "content") → isinstance(content, str)   → yield content
                            → isinstance(content, list)   → yield text items only
                            → else                        → yield str(content)
② isinstance(chunk, str)                                 → yield chunk
③ 以上都不匹配                                            → 不 yield（静默跳过）
```

这避免了直接 `.content` 在非标准返回类型上抛 `AttributeError`。

---

### 4.5 `get_checkpointer() -> MemorySaver`

**功能**：返回内部 checkpointer，供外部按 thread_id 查询对话历史。

**用法**：

```python
checkpointer = agent.get_checkpointer()
state = await agent._agent.aget_state(
    {"configurable": {"thread_id": "session-1"}}
)
for msg in state.values["messages"]:
    print(f"{msg.type}: {msg.content}")
```

---

### 4.6 `async close()`

**功能**：释放 MCP SSE 连接和所有资源。

调用方应始终用 `try/finally` 确保释放：

```python
agent = StockAnalysisAgent("config/mcp_config.json")
try:
    await agent.initialize()
    async for chunk in agent.chat("你好"):
        ...
finally:
    await agent.close()
```

---

## 5. Checkpointer 与多轮对话

### 5.1 工作原理

```
Chat 1: "茅台股价"     thread_id="session-1"
  └─ checkpoint_1: [HumanMsg("茅台"), AIMsg("12.58元")]

Chat 2: "PE呢？"       thread_id="session-1"  ← 同一个
  └─ checkpointer 恢复 checkpoint_1
  └─ Agent 看到完整上下文 → 理解 "PE" 指茅台 PE
  └─ checkpoint_2: [...完整历史..., AIMsg("PE=15.2")]
```

### 5.2 何时创建新 thread_id

- **新会话** → 新 `thread_id`（如每次 HTTP 请求生成 UUID）
- **同一用户连续对话** → 相同 `thread_id`
- **重置上下文** → 新 `thread_id`

---

## 6. 错误处理策略

| 阶段 | 错误场景 | 处理 |
|------|----------|------|
| 构造 | `mcp_config.json` 不存在 | 抛出 `FileNotFoundError` |
| 初始化 | `api_key` 为空 | 抛出 `ValueError`（明确提示配置方式） |
| 初始化 | MCP SSE 连接失败 | 抛出 MCP SDK 原始异常 |
| 初始化 | LLM 创建失败 | 抛出 `ChatOpenAI` 原始异常 |
| 对话 | LLM 调用认证失败 | 抛出 `AuthenticationError` |
| 对话 | 工具调用超时 | 抛出 MCP SDK 超时异常 |
| 清理 | `close()` 中服务器释放失败 | `try/except` 吞掉，不影响其他服务器 |

---

## 7. 使用示例

### 基本用法

```python
from src.agents.stock_agent import StockAnalysisAgent

agent = StockAnalysisAgent("config/mcp_config.json")
await agent.initialize()

# 流式对话
async for chunk in agent.chat("分析一下茅台"):
    ...

await agent.close()
```

### 多轮对话（带记忆）

```python
thread = {"configurable": {"thread_id": "user-123"}}

# 第一轮
async for event in agent._agent.astream_events(
    {"messages": [HumanMessage(content="茅台估值")]},
    config=thread, version="v2",
):
    if event["event"] == "on_chat_model_stream":
        print(event["data"]["chunk"].content, end="")

# 第二轮——Agent 记得上一轮讨论过茅台
async for event in agent._agent.astream_events(
    {"messages": [HumanMessage(content="再看看五粮液")]},
    config=thread, version="v2",
):
    ...

# 查看历史
state = await agent._agent.aget_state(thread)
```

### 集成测试

```bash
python -m src.agents.stock_agent
```

执行 `main()`：创建 → 两轮对话 → 打印历史 → 销毁。

---

## 8. 关键设计决策

### 8.1 为什么延迟初始化

构造时不建立连接，`initialize()` 才真正连接 MCP 和创建 LLM。好处：

- 构造参数校验与 I/O 操作分离
- 调用方可以在构造后、`initialize()` 前做额外配置
- 异步操作在明确的 `await` 点触发，不隐藏在构造函数中

### 8.2 为什么 `chat()` 不传 config

`chat()` 是简化接口，适合单轮或无需记忆的场景。多轮对话时，调用方直接使用 `agent._agent.astream(..., config=...)` 获得完整控制权。这避免了在 `chat()` 上增加 `thread_id` 参数导致接口臃肿。

### 8.3 为什么 api_key 在初始化阶段校验

如果在 `ChatOpenAI()` 构造时不校验，空 `api_key` 不会立即报错。只有到第一次 `chat()` 调用 LLM 时才会抛 `AuthenticationError`——此时距离初始化可能已经过了很久，排查根因需要回溯到配置阶段。尽早校验缩小了错误半径。

### 8.4 为什么 MemorySaver 而非外部数据库

`MemorySaver` 存在进程内存中：
- **优势**：零依赖、零配置、适合开发和单机部署
- **局限**：进程重启后丢失、不支持多实例共享
- **升级路径**：`MemorySaver` → `SqliteSaver`（持久化）→ `PostgresSaver`（生产级），接口完全兼容，仅替换实现即可
