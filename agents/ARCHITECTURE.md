# Agent 架构

企业问答编排基于 LangChain `create_agent` + 7 个自定义钩子函数，没有自写中间件类，也没有手写图节点。

## 分层

- **授权层决定怎么写**：LangChain，用 `create_agent`、钩子装饰器、`@tool`。
- **运行时层负责怎么跑**：LangGraph，用 `Command` 改图 state、`InjectedState` 注入身份、`add_messages` reducer。

## 流程

```
run_agent(user_message, history, department, allow_all)
  └─ create_agent(model, tools, system_prompt, state_schema, middleware)
       rag_hook (@before_model)   入口检索：部门过滤 → 文本[知识库参考] + 图片多模态，sources=[N] 顺序
       ┌──────── Agent Loop: model ⇄ tools ─────────┐
       │ model_step_log_hook (@wrap_model_call)  记录 llm 决策与 usage   │
       │ max_steps_hook (@after_model, jump_to=end)  ToolMessage≥5 收尾  │
       │ tools 洋葱，外→内：exact_source → collect_sources              │
       │                    → prune_knowledge → tool_logging → 工具本体 │
       └───────────────────────────────────────────────────────────────┘
```

`create_agent` 每请求现建，避免缓存干扰测试里的模型替换。

## 钩子职责

| 钩子 | 挂载点 | 作用 |
|---|---|---|
| rag_hook | `@before_model` | 每次模型调用前完成检索注入，组装 sources |
| model_step_log_hook | `@wrap_model_call` | 包住模型调用以拿到 usage 与决策 |
| max_steps_hook | `@after_model` | 按 ToolMessage 计数收尾，等价旧条件边 |
| tool_logging_hook | 工具最内层 | 时长只算工具本体，异常在此归一化 |
| prune_knowledge_hook | 工具洋葱 | 经 `Command` 删模型请求视图里的知识块 |
| collect_sources_hook | 工具洋葱 | 读 `ToolMessage.artifact` 回传的来源 |
| exact_source_hook | 工具最外层 | 收集/重置后追加精确来源卡，保持顺序 |

模型降级保留 `FallbackChatModel`，官方件对 4xx 也降级，不符生产语义；工具重试保留工具内部
`retry_async`，换中间件会改变重试层次并破坏日志契约；轮次上限由 `max_steps_hook` 实现，
逐行等价原条件边。
