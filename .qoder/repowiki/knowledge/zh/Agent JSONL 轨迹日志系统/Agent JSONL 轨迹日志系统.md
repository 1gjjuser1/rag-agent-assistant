---
kind: logging_system
name: Agent JSONL 轨迹日志系统
category: logging_system
scope:
    - '**'
source_files:
    - utils/logger.py
    - react_agent.py
    - rag_pipeline.py
    - tests/test_context.py
---

## 1. 使用的系统与框架

仓库没有引入 Python 标准 `logging` 模块或第三方日志库（如 loguru、structlog），而是实现了一个**轻量级专用日志器**：`utils/logger.py` 中的 `AgentTraceLogger`。该日志器以 **JSON Lines (JSONL)** 格式将 Agent 执行轨迹逐行追加写入文件，默认路径为 `data/agent_trace.jsonl`。

此外，业务代码中广泛使用 `print()` 作为调试/演示输出（例如 `llm_client.py`、`vector_store.py`、`rag_pipeline.py`、`react_agent.py` 的 `__main__`），这些不属于结构化日志体系，仅用于交互式演示和快速排障。

## 2. 核心文件与位置

- `utils/logger.py`：定义 `AgentTraceLogger` 类与 `estimate_tokens()` 辅助函数，是日志系统的唯一实现。
- `react_agent.py`：`ReActAgent` 通过依赖注入接收 `AgentTraceLogger`，在每轮工具调用前后调用 `_record()` 写入轨迹。
- `rag_pipeline.py`：仅复用 `estimate_tokens()` 做上下文 token 估算，不直接写日志。
- `tests/test_context.py`：测试中也 import 了 `estimate_tokens`，验证其估算逻辑。

## 3. 架构与设计决策

### 3.1 单例式线程安全写入
`AgentTraceLogger` 使用类级 `threading.Lock()`（`_lock = threading.Lock()`）保证多 Agent 实例并发写入同一 JSONL 文件时不会交错行。每次 `log()` 调用都先 `mkdir(parents=True, exist_ok=True)` 确保目录存在，再以 `a` 模式追加写入。

### 3.2 结构化字段设计
每条记录包含以下固定字段：
- `timestamp`：UTC ISO 时间戳（`datetime.now(timezone.utc).isoformat()`）
- `step`：整数步号（从 1 递增）
- `thought_summary`：人类可读的步骤摘要
- `tool`：调用的工具名（无工具时为 `None`）
- `args`：工具参数字典
- `observation`：工具返回结果（截断至 `TOOL_OBSERVATION_MAX_CHARS=2000` 字符）
- `cost_estimate`：基于 `estimate_tokens(prompt, observation)` 估算的 token 成本

### 3.3 故障隔离原则
`log()` 方法用 `try/except Exception: pass` 包裹全部 I/O 操作，注释明确声明“日志系统不得拖垮业务流程”。任何写入失败（磁盘满、权限问题等）都会被静默吞掉，不影响 Agent 主循环。

### 3.4 调用点集中化
`ReActAgent._record()` 是唯一对外暴露的写入入口，`run_stream()` 的两条分支（模型未调用工具 / 调用工具）都统一经过该方法，避免散落多处写日志逻辑。

## 4. 约定与约束

| 约定 | 说明 | 来源 |
|---|---|---|
| 轨迹文件路径 | 默认 `data/agent_trace.jsonl`，可通过构造参数覆盖 | `AgentTraceLogger.__init__` |
| 编码 | UTF-8 | 文件打开参数 `encoding="utf-8"` |
| 写入模式 | 追加（append），不覆盖历史轨迹 | `open("a", ...)` |
| 并发安全 | 通过类级 `threading.Lock` 串行化写入 | `_lock` 与 `with self._lock` |
| 异常策略 | 所有日志异常被捕获并忽略，绝不抛出 | `try/except Exception: pass` |
| 字段类型 | `step`、`cost_estimate` 强制 `int`；`tool` 可为 `None` | `log()` 参数转换 |
| 观察值长度 | 工具返回结果在写入前被截断到 2000 字符 | `TOOL_OBSERVATION_MAX_CHARS` 常量 |
| Token 估算 | 中文按 1 token/字，英文/数字按 4 字符/token，下限为 1 | `estimate_tokens()` 实现 |
| 非结构化输出 | 除 `AgentTraceLogger` 外，其余模块使用 `print()` 进行控制台输出，不属于结构化日志体系 | 各模块 `__main__` 与示例代码 |

## 5. 适用范围说明

该日志系统**仅服务于 Agent 执行轨迹追踪**，并未提供通用应用日志能力（如 INFO/WARNING/ERROR 分级、控制台/文件双 sink、请求链路 ID 等）。其他模块（RAG 管线、向量存储、LLM 客户端、评测脚本）均直接使用 `print()` 输出，没有统一的日志门面。因此本卡片描述的范围严格限定在 `utils/logger.py` 及其消费者 `react_agent.py`。