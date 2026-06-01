# LangGraph create_agent 工作原理

## 1. 一句话总结

`create_agent` 封装了完整的 **ReAct 循环**——你只需提供 tools、system_prompt 和 model，LangGraph 自动处理工具调度、状态管理和循环控制。

---

## 2. ReAct 循环机制

`create_agent` 编译出的 `CompiledStateGraph` 内部是一个自动循环：

```
用户输入
   │
   ▼
┌──────────┐    有 tool_calls    ┌──────────┐
│  LLM     │ ──────────────────→ │  Tools   │
│  (model) │                     │  (tools) │
└──────────┘ ←────────────────── └──────────┘
   │         工具结果反馈给 LLM
   │
   │ 无 tool_calls
   ▼
  END（返回最终回复）
```

**流程**：LLM 分析用户问题 → 决定调用某个工具 → 工具返回结果 → 结果喂回 LLM → LLM 判断是否需要继续调用 → 直到信息充分，生成最终回复。

**实际案例**（来自项目运行日志）：

```
用户: "收集 江苏金租 最近一周的事件，并分析其影响"

① LLM 决定: 需要搜索事件
   🔧 search_events({'keyword': '江苏金租', 'days': 7})
   ✅ 工具返回（15 条事件）

② LLM 决定: 需要做事件分析
   🔧 analyze_event({'event_description': None})
   ❌ 参数错误，LLM 看到错误

③ LLM 修正参数后重试
   🔧 analyze_event({'event_description': '跨境租赁业务突破...'})
   ✅ 工具返回（完整分析报告）

④ LLM 综合所有结果，生成最终回复
```

一个 prompt 触发了 3 次工具调用，全部由 LangGraph 自动完成。

---

## 3. 其他 Agent 模式

除了 ReAct，LangGraph 还支持以下模式：

| 模式 | 核心思想 | 适用场景 |
|------|----------|----------|
| **ReAct**（默认） | 思考→行动→观察→思考，循环到底 | 需要灵活调用工具的任务 |
| **Plan-and-Execute** | 先制定完整计划，再逐步执行，执行中可根据结果动态调整 | 多步骤、可能需要中途调整的任务（如研究分析、多对象对比） |
| **Reflection** | 生成→自我审视→修正，可多轮迭代 | 有明确质量标准、需要打磨的任务（如代码生成、文章润色） |
| **Tool-calling** | LLM 直接输出函数调用，推理隐含在内部 | 简单工具调用、追求低延迟的场景 |
| **Multi-Agent / Supervisor** | 多个 Agent + 一个调度者 | 复杂、多角色的任务（如客服+销售+售后协同） |

### 实际案例对比：分析 5 只股票的估值
```
**ReAct 模式**（你当前的实现）：
1. 分析茅台 → 调用工具 → 得到结果
2. 分析五粮液 → 调用工具 → 得到结果
3. 分析泸州老窖 → ...
4. 汇总 5 份独立分析

**Plan-and-Execute 模式**：
1. 规划：需要收集 5 只股票的 PE、PB、ROE
2. 执行：并行调用 5 次 get_financials
3. 汇总：统一对比分析输出

→ Plan-and-Execute 更高效，结果更适合对比
```

### 对比示例（同一问题在不同模式下）

```
ReAct（逐步试错）
────────
用户: "茅台怎么估值？"
  思考: 我需要股价、PE、PB
  行动: get_market_data("600519")
  观察: PE=25, PB=4.5
  思考: 还需要行业对比
  行动: search_events("白酒 PE")
  观察: 行业 PE=30
  回答: 综合数据给出结论

Plan-and-Execute（先规划后执行）
────────
用户: "茅台怎么估值？"
  计划: ①行情 ②行业 ③DCF ④结论
  执行: 依次完成 1-4
  回答: 汇总结果

Reflection（自我审查）
────────
用户: "茅台怎么估值？"
  生成: "茅台 PE 约 25 倍，估值合理"
  审视: "缺少 DCF 和行业对比"
  修正: 补充后给出完整分析
  回答: 更全面的结论
```

---

## 4. 你只需提供什么

LangGraph 的核心理念是**关注业务，框架负责实现**：

```
你需要提供的                    LangGraph 自动提供的
───────────────                ────────────────────

tools（工具列表）               循环控制（何时继续、何时停止）
┌──────────┐                  状态管理（messages、checkpoint）
│ MCP 工具  │                  工具调度（调用哪个、参数校验）
└──────────┘                  错误恢复（工具失败 → 自动重试）
                              流式输出（token by token）
system_prompt（行为规范）
┌──────────┐
│ 提示词    │
└──────────┘

model（LLM）
┌──────────┐
│ 模型      │
└──────────┘
```

---

## 5. 在 StockAnalysisAgent 中的实际配置

四行代码完成 Agent 创建：

```python
self._agent = create_agent(
    model=llm,                        # ① LLM 选择
    tools=tools,                      # ② 可用工具（MCP 动态发现）
    system_prompt=SYSTEM_PROMPT,      # ③ 行为规范
    checkpointer=self._checkpointer,  # ④ 记忆管理（SQLite 持久化）
)
```

之后只需一行调用：

```python
async for chunk in self._agent.astream_events(
    {"messages": [HumanMessage(content=user_input)]},
    config={"configurable": {"thread_id": thread_id}},
    version="v2",
):
    # 流式消费 Agent 输出
```

所有工具调用的循环、状态管理、流式输出均由 LangGraph 内部完成。

---

## 6. 模式叠加：Middleware 机制

`create_agent` 支持通过 `middleware` 参数叠加功能，扩展 Agent 能力：

```python
from langchain.agents.middleware import (
    SummarizationMiddleware,   # 自动摘要历史对话
    ToolRetryMiddleware,       # 工具调用失败自动重试
    LoggingMiddleware,         # 记录 Agent 执行日志
)

self._agent = create_agent(
    model=llm,
    tools=tools,
    system_prompt=SYSTEM_PROMPT,
    checkpointer=self._checkpointer,
    middleware=[
        SummarizationMiddleware(max_tokens_before_summary=4000),
        ToolRetryMiddleware(max_retries=3),
        LoggingMiddleware(),
    ]
)
```

这样，从基础的 ReAct 出发，可以逐步叠加能力，升级为更复杂的 Agent 架构。

---

## 7. Checkpointer 的作用

`checkpointer` 不仅仅是"记忆管理"，它还负责：

| 功能 | 说明 |
|------|------|
| **对话持久化** | SQLite 存储，进程重启后对话不丢失 |
| **状态快照** | 可回滚到任意历史状态 |
| **多会话隔离** | 不同 `thread_id` 的数据完全隔离 |
| **时间旅行** | 可查看、恢复历史对话节点 |

这就是你在项目中选择 `AsyncSqliteSaver` 的原因——轻量、持久化、开箱即用。

---

## 8. 关键结论

1. **不需要自己实现 ReAct**：`create_agent` 内置完整的思考-行动循环
2. **工具即能力**：Agent 能力由 tools 列表定义，LangGraph 自动调度
3. **提示词即行为**：system_prompt 定义 Agent 的"人设"和规则
4. **模式可叠加**：从 ReAct 出发，通过 middleware/subgraphs 可升级为更复杂的 Agent 架构
5. **Checkpointer 是关键**：它不仅管理记忆，还支持持久化、快照和时间旅行