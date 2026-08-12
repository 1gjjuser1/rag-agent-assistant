---
kind: error_handling
name: 分层错误处理：FastAPI HTTPException + 业务 ValueError + safe_error 统一降级
category: error_handling
scope:
    - '**'
source_files:
    - api.py
    - app.py
    - llm_client.py
    - rag_pipeline.py
    - react_agent.py
    - utils/logger.py
---

## 1. 整体方案

本项目采用**分层错误处理**策略，按调用栈从外到内分为三层：

- **HTTP 层（`api.py`）**：FastAPI 路由统一通过 `raise HTTPException(status_code, detail=...)` 把内部异常映射为 REST 状态码；鉴权失败返回 401、知识库/文档不存在返回 404、参数校验失败返回 400、重复创建返回 409、未知异常兜底返回 500。
- **应用层（`rag_pipeline.py`、`react_agent.py`）**：核心业务方法不抛异常，而是返回包含 `error` / `answer` 字段的字典或 `ToolResult`，由上层决定如何呈现；工具执行失败时以 `safe_error(exc)` 包装后回填给模型，让 Agent 自行重试或降级。
- **基础设施层（`llm_client.py`）**：所有外部 LLM 调用被 `DashScopeClient` 捕获并统一转换为 `RuntimeError`，附带 model/base_url 上下文信息，便于排障。

项目**没有自定义异常类**（如 `class XxxError(Exception)`），也没有使用 `try/except` 定义领域错误类型——业务边界用 Python 内置 `ValueError` 表示参数非法，其余异常一律走通用 `Exception` 路径。

## 2. 关键文件与职责

| 文件 | 错误处理职责 |
|---|---|
| `api.py` | FastAPI 路由入口；`require_auth` 抛出 401；`_ensure_kb` 抛出 404；`create_kb` 捕获 `ValueError` 转 409；`upload_document` 捕获 `ValueError` 转 400；`chat` 捕获 `Exception` 转 500，并使用 `safe_error` 脱敏 |
| `app.py` | Streamlit UI；后台入库线程用 `try/except Exception` 记录 `job.error`，UI 通过 `st.error` / `st.warning` 展示；上传失败逐文件 `st.warning` 提示 |
| `llm_client.py` | `DashScopeClient` 把所有外部异常包装为 `RuntimeError`；`safe_error` 仅输出 `"异常类名: 消息"`，隐藏堆栈 |
| `react_agent.py` | `ToolRegistry.call` 捕获工具异常并以 `ToolResult(content=f"工具执行失败：{safe_error(exc)}")` 返回，让模型继续循环；`run_stream` 的 LLM 调用异常产出 `{"type": "error", ...}` 事件 |
| `rag_pipeline.py` | `ingest` 对每个文档 try/except 收集 `errors` 列表，最终在 `message` 中汇总；`answer` / `chat` 捕获异常返回带 `safe_error` 的错误回答；`rewrite_query` 异常时静默退回原问题 |
| `utils/logger.py` | `AgentTraceLogger.log` 用 `try/except Exception: pass` 保证日志写入失败不影响主流程 |

## 3. 架构约定与设计决策

1. **对外 API 只暴露 HTTP 语义**：所有业务异常在 `api.py` 中被转换为 `HTTPException`，下游消费者只看到标准状态码和 `detail` 字符串。`from exc` 保留原始异常链以便服务端日志追踪。
2. **业务层“吞异常”而非抛出**：`RAGPipeline.answer`、`RAGPipeline.chat`、`RAGPipeline.rewrite_query`、`ReActAgent._weather`、`ReActAgent._query_database` 等核心方法全部 `try/except Exception` 并返回结构化结果（含 `answer` 字段），确保 RAG/Agent 管线不会因为单个组件故障而中断整个请求。
3. **安全脱敏**：所有向用户可见的错误信息都经过 `safe_error(exc)`，仅保留 `"异常类名: 异常消息"`，不泄露堆栈、路径、密钥等敏感信息。
4. **日志系统不可阻塞主流程**：`AgentTraceLogger.log` 明确注释“日志系统不得拖垮业务流程”，写入失败直接 `pass`。
5. **无全局中间件**：FastAPI 未注册 `exception_handler`，错误转换逻辑内联在每个路由中，简单直接但缺乏集中式统一处理。
6. **Streamlit 侧就地反馈**：UI 层不使用异常传播，而是通过 `st.error` / `st.warning` / `st.info` 即时展示，后台任务状态保存在 `IngestJob` dataclass 中由工作线程写入、主线程读取。
7. **Agent 工具级容错**：工具执行异常不会中断 ReAct 循环，而是以自然语言形式回传给模型，让模型有机会换参数重试或直接给出答案。

## 4. 约定与约束（基于代码观察）

- **HTTP 层**：所有 `/v1/*` 路由必须经 `Depends(require_auth)` 鉴权；鉴权失败固定返回 401。
- **参数校验**：Pydantic `BaseModel` 负责输入校验（如 `ChatRequest.question` 的 `min_length=1`），非法输入由 FastAPI 自动返回 422；业务层额外用 `if not body.question.strip()` 返回 400 的中文提示。
- **资源不存在**：知识库/文档不存在统一返回 404，消息格式为 `f"...不存在：{id}"`。
- **幂等性保护**：重复创建同名知识库返回 409（捕获 `ValueError`）。
- **异步/并发**：服务进程内使用 `threading.RLock()` 串行化 SQLite/Chroma 访问，避免并发写冲突；后台入库使用独立线程 + `contextlib.suppress(Exception)` 刷新实例。
- **LLM 调用**：所有外部调用必须通过 `DashScopeClient`，禁止直接调用 OpenAI SDK，以保证异常统一包装。
- **测试覆盖**：测试套件（`tests/`）通过注入 Fake LLM/存储验证正常路径，未见专门针对异常分支的断言，说明错误路径尚未被显式覆盖。

## 5. 缺失与风险

- 没有统一的异常基类或错误码枚举，新增错误类型时需开发者自行判断应抛 `ValueError` 还是 `HTTPException`。
- 错误处理分散在各路由和业务方法中，缺少集中式 `@app.exception_handler`，难以统一添加审计日志或指标上报。
- 未使用 `logging` 模块记录错误（除 JSONL 轨迹日志外），生产环境排障依赖 FastAPI/Uvicorn 默认日志。
- 未使用 `pydantic` 的 `ValidationError` 自定义响应结构，客户端需解析 FastAPI 默认的 `{"detail": [...]}` 格式。