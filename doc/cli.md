# CLI 设计文档

## 1. 概述

CLI（Command Line Interface）是 A 股分析大师的终端交互入口。提供流式对话、多轮会话管理、命令系统。架构上分为两层：

```
┌─────────────────────────────────────────┐
│              cli/main.py                │
│          StockAgentCLI                  │
│                                         │
│  ① 启动 → ② 主循环 → ③ 清理            │
│  ┌──────┐ ┌──────────┐ ┌──────────┐   │
│  │init  │ │  run()   │ │cleanup() │   │
│  └──────┘ └────┬─────┘ └──────────┘   │
│                │                       │
│      ┌─────────┼──────────┐            │
│      │         │          │            │
│  普通消息   命令(/...)   异常           │
│      │         │          │            │
│  _process   _handle   错误处理         │
│  _message   _command                   │
│      │         │                       │
└──────┼─────────┼───────────────────────┘
       │         │
       │    ┌────▼──────────────────────┐
       │    │    cli/commands.py        │
       │    │    handle_command()       │
       │    │                          │
       │    │  /new      → _cmd_new    │
       │    │  /exit     → _cmd_exit   │
       │    │  /history  → _cmd_history│
       │    │  /threads  → _cmd_threads│
       │    │  /switch   → _cmd_switch │
       │    │  /clear    → _cmd_clear  │
       │    │  /help     → _cmd_help   │
       │    └──────────────────────────┘
       │
  ┌────▼────────────────────────────────┐
  │     src/agents/stock_agent.py       │
  │     StockAnalysisAgent              │
  │                                     │
  │  chat_with_tool_feedback()          │
  │  get_thread_history()               │
  │  list_threads()                     │
  │  delete_thread()                    │
  └─────────────────────────────────────┘
```

---

## 2. 模块结构

| 文件 | 职责 | 行数 |
|------|------|------|
| `cli/main.py` | 启动、主循环、多行输入、流式输出、资源清理 | ~130 |
| `cli/commands.py` | 命令路由、7 个命令实现、提示文本常量 | ~160 |
| `cli/__init__.py` | 包标记 | 0 |

---

## 3. `cli/main.py` — UI 层

### 3.1 `StockAgentCLI` 类

#### 字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `agent` | `StockAnalysisAgent \| None` | Agent 实例，`initialize()` 中创建 |
| `current_thread_id` | `str` | 当前活跃的会话 ID，默认 `"default"` |
| `running` | `bool` | 主循环开关，`/exit` 设为 `False` |

#### 生命周期

```
__init__()  →  initialize()  →  run()  →  cleanup()
   │               │              │           │
  构造字段      创建 Agent    主循环        释放资源
              打印 TIPS     try/finally   关闭 Agent
```

### 3.2 `initialize()`

1. 打印启动横幅
2. 创建 `StockAnalysisAgent`（配置路径 `config/mcp_config.json`）
3. 调用 `await agent.initialize()` 加载工具和 LLM
4. 打印 `TIPS`（从 `commands.py` 导入）

### 3.3 `run()` — 主循环

```
while self.running:
    user_input ← _get_user_input()

    if user_input.startswith("/"):
        _handle_command(user_input)  →  commands.handle_command()
    else:
        _process_message(user_input)

finally:
    cleanup()
```

**错误处理策略**：
- `KeyboardInterrupt` → 打印 "再见" 并退出循环 → `finally` 触发 `cleanup()`
- 其他 `Exception` → 打印错误，继续循环（不阻断使用）

### 3.4 `_get_user_input() -> str`

多行输入模式：

```
空行结束：      两次回车 = 结束多行
取消输入：      单行 `.` + 回车 = 返回空字符串
单行模式：      输入一行后按回车 = 直接返回
多行模式：      连续输入多行，空行结束 → 返回 "\n".join(lines)
```

提示符格式：`💬 [thread_id] > `，当前会话 ID 始终可见。

### 3.5 `_process_message(user_input)`

1. 调用 `agent.chat_with_tool_feedback(user_input, thread_id=self.current_thread_id)`
2. 每个 chunk 用 `print(chunk, end="", flush=True)` 流式输出
3. 工具调用日志（`🔧 工具名` / `❌ 错误`）由 `chat_with_tool_feedback` 内部打印

### 3.6 `_handle_command(command)`

一行委托：

```python
self.current_thread_id, self.running = await handle_command(
    command, self.agent, self.current_thread_id
)
```

### 3.7 `cleanup()`

```python
if self.agent:
    await self.agent.close()
```

释放 MCP SSE 连接和 SQLite 数据库连接。

---

## 4. `cli/commands.py` — 命令层

### 4.1 状态契约

每个命令函数返回 `(thread_id: str, running: bool)`：

| 命令 | thread_id | running | 说明 |
|------|-----------|---------|------|
| `/new [id]` | 新 ID | `True` | 可能改变 thread_id |
| `/switch <id>` | 新 ID | `True` | 改变 thread_id |
| `/exit` | 不变 | **`False`** | 终止主循环 |
| 其他 | 不变 | `True` | 仅打印信息 |

### 4.2 `handle_command(command, agent, thread_id) -> (str, bool)`

命令路由函数。匹配顺序：

```
/exit 或 /quit  →  _cmd_exit()
/new 或 /new X  →  _cmd_new()
/history       →  _cmd_history()
/threads       →  _cmd_threads()
/switch X      →  _cmd_switch()
/clear         →  _cmd_clear()
/help          →  _cmd_help()
其他            →  "未知命令"
```

### 4.3 命令详解

#### `/new [id]`

```
/new              → 生成 "UUID[:12]" → 创建并切换
/new my-analysis  → 检查是否存在
                      ├─ 不存在 → 直接创建
                      └─ 已存在 → ❌ 报错，提示 /switch
```

#### `/history`

显示当前 `thread_id` 的完整对话历史。消息类型映射：

| `msg.type` | 显示 |
|-----------|------|
| `human` | 👤 用户 |
| `ai` | 🤖 助手 |
| `tool` | 🔧 工具 |

内容过长时自动截断（用户消息 200 字符，工具消息 100 字符）。

#### `/threads`

列出所有活跃的会话 ID，当前会话前面标记 `👉`。

#### `/switch <id>`

直接切换到指定的 `thread_id`。不检查是否存在——切换到不存在的 ID 会在下一次对话时自动创建。

#### `/clear`

调用 `agent.delete_thread(thread_id)` 删除当前会话的全部历史。删除后 `thread_id` 不变，后续对话视为新会话。

#### `/help`

打印 `COMMAND_HELP` 常量。

#### `/exit`

设置 `running = False`，主循环退出后 `cleanup()` 执行资源释放。

---

## 5. 提示文本

| 常量 | 使用位置 | 说明 |
|------|----------|------|
| `TIPS` | `initialize()` 结尾 | 启动后一次性显示 |
| `COMMAND_HELP` | `/help` 命令 | 完整命令参考 |

两个常量定义在 `commands.py` 中统一管理，便于修改和国际化。

---

## 6. 数据流

```
用户输入 "分析茅台"
        │
        ▼
  _get_user_input()
        │
        ▼
  _process_message("分析茅台")
        │
        ▼
  agent.chat_with_tool_feedback("分析茅台", thread_id="default")
        │
        ├─ LLM 决定调用 get_market_data(symbol="600519")
        │     └─ 打印: 🔧 get_market_data(...)
        │              ✅ 工具返回
        │
        └─ LLM 生成最终回复
              └─ yield "茅台当前股价..."
                    │
                    ▼
              print("茅台当前股价...", end="", flush=True)  ← 逐 token 打印
```

---

## 7. 错误处理

| 阶段 | 异常 | 处理 |
|------|------|------|
| `initialize()` | 任何异常 | 向上传播到 `main()`，打印错误并 `sys.exit(1)` |
| `_get_user_input()` | `KeyboardInterrupt` | re-raise → `run()` 中 catch → 退出循环 |
| `_process_message()` | LLM 错误 / 网络超时 | catch `Exception` → 打印 `❌ 处理失败` → 继续循环 |
| `_handle_command()` | 命令内部异常 | catch `Exception` → 打印 `❌ 错误` → 继续循环 |
| `cleanup()` | 连接释放失败 | 由 `MCPToolAdapter.close()` 内部 `try/except` 处理 |

**关键设计**：`cleanup()` 在 `run()` 的 `finally` 块中调用，确保无论主循环如何退出（正常 / 异常 / Ctrl+C），资源都会被释放。

---

## 8. 运行方式

```bash
# 从项目根目录运行
python -m cli.main
```

```bash
# 或直接执行（需要 chmod +x）
./cli/main.py
```
