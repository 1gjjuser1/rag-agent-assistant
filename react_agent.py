"""基于 Function Calling 的轻量 Agent：工具注册表 + 多步循环 + 流式输出。

相比原版“关键词路由 + 单次调用”，阶段 A 的 Agent 做了三点本质改进：

1. **LLM 自己选工具**：把工具以 JSON Schema 形式交给模型，模型根据用户问题
   决定调用哪个工具、传什么参数（真正的 ReAct observe → think → act 循环）；
2. **多步循环**：模型可连续调用多个工具（如先查知识库、再查天气），每轮把
   工具结果回填给模型，直到模型给出最终回答或达到最大轮数；
3. **可插拔工具注册表**：新增工具只需实现一个普通函数并用 ``@registry.register``
   注册，无需改动循环逻辑。

流式输出通过 :meth:`ReActAgent.run_stream` 以事件字典序列呈现
（``tool_start`` / ``tool_end`` / ``token`` / ``done``），UI 可逐帧渲染。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from llm_client import DashScopeClient, safe_error
from rag_pipeline import LLMClient, RAGPipeline, truncate_history
from utils.logger import AgentTraceLogger, estimate_tokens

WEATHER_CODES = {
    0: "晴",
    1: "大致晴朗",
    2: "局部多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "中等毛毛雨",
    55: "强毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    80: "小阵雨",
    81: "中等阵雨",
    82: "强阵雨",
    95: "雷暴",
    96: "雷暴伴小冰雹",
    99: "雷暴伴大冰雹",
}

OPEN_METEO_GEOCODING_URL = os.getenv(
    "OPEN_METEO_GEOCODING_URL",
    "https://geocoding-api.open-meteo.com/v1/search",
).rstrip("?")
OPEN_METEO_FORECAST_URL = os.getenv(
    "OPEN_METEO_FORECAST_URL",
    "https://api.open-meteo.com/v1/forecast",
).rstrip("?")

# 工具返回结果回填给模型的最大长度：防止联网搜索等长结果灌爆上下文、
# 稀释注意力（结果截断后仍足够模型理解要点）。
TOOL_OBSERVATION_MAX_CHARS = 2000


@dataclass
class AgentResult:
    answer: str
    sources: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ToolResult:
    """工具返回值：content 回填给模型，sources 用于 UI 展示引用。"""

    content: str
    sources: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema
    function: Callable[..., ToolResult]


class ToolRegistry:
    """工具注册表：注册 → 生成 OpenAI tools 格式 → 按名调用。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
    ) -> Callable[[Callable[..., ToolResult]], Callable[..., ToolResult]]:
        def decorator(func: Callable[..., ToolResult]) -> Callable[..., ToolResult]:
            if name in self._tools:
                raise ValueError(f"工具重名：{name}")
            self._tools[name] = ToolSpec(name, description, parameters, func)
            return func

        return decorator

    def specs(self) -> list[dict[str, Any]]:
        """OpenAI 兼容的 tools 参数。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            }
            for spec in self._tools.values()
        ]

    def names(self) -> list[str]:
        return list(self._tools)

    def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        spec = self._tools.get(name)
        if spec is None:
            return ToolResult(
                content=f"未知工具：{name}，可用工具：{', '.join(self.names())}"
            )
        try:
            result = spec.function(**arguments)
            return result if isinstance(result, ToolResult) else ToolResult(content=str(result))
        except Exception as exc:
            # 工具异常回填给模型，让它有机会换参数重试或直接回答。
            return ToolResult(content=f"工具执行失败：{safe_error(exc)}")


class ReActAgent:
    """Function Calling Agent：LLM 选工具 → 执行 → 回填 → 循环。"""

    def __init__(
        self,
        rag: RAGPipeline | None = None,
        llm_client: LLMClient | None = None,
        logger: AgentTraceLogger | None = None,
        max_steps: int | None = None,
    ) -> None:
        self.llm = llm_client or DashScopeClient()
        self.rag = rag or RAGPipeline(llm_client=self.llm)
        self.logger = logger or AgentTraceLogger()
        self.max_steps = max_steps or self.rag.config.agent_max_steps
        self.agent_system_prompt = (
            "你是公司的智能助手。你可以使用工具获取知识库、天气或联网信息；"
            "工具结果不足时如实说明，不要编造。回答使用中文，引用来源。"
        )

    # ---------- 工具定义 ----------

    def _build_registry(self, kb_id: str | None) -> ToolRegistry:
        registry = ToolRegistry()

        def search_knowledge_base(query: str) -> ToolResult:
            result = self.rag.answer(query, kb_id=kb_id)
            return ToolResult(
                content=result["answer"],
                sources=result["sources"],
                extra={"context_tokens": result.get("context_tokens", 0)},
            )

        def search_web(query: str) -> ToolResult:
            return ToolResult(content=self.llm.web_search(query))

        def get_weather(city: str) -> ToolResult:
            return ToolResult(content=self._weather(city))

        registry.register(
            "search_knowledge_base",
            "在公司的知识库中检索文档并给出带引用来源的回答。"
            "任何与公司文档、制度、产品、项目、简历、汇报有关的问题都应优先使用此工具。",
            {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "用于检索知识库的问题，应完整、明确，可包含关键术语。",
                    }
                },
                "required": ["query"],
            },
        )(search_knowledge_base)

        registry.register(
            "get_weather",
            "查询某个城市当前天气与当日预报，数据来自 Open-Meteo，无需 API Key。",
            {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "城市名，例如：北京、上海、郑州。",
                    }
                },
                "required": ["city"],
            },
        )(get_weather)

        registry.register(
            "search_web",
            "通过搜索引擎获取实时信息（最新新闻、实时行情、时效性内容）。",
            {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "要搜索的关键词或问题。",
                    }
                },
                "required": ["query"],
            },
        )(search_web)

        def query_database(sql: str) -> ToolResult:
            return ToolResult(content=self._query_database(sql))

        registry.register(
            "query_database",
            "对公司本地 SQLite 数据库执行只读 SQL 查询（仅 SELECT/PRAGMA/EXPLAIN/WITH），"
            "用于查知识库、文档、片段数量或任意元数据；数据库以只读模式打开，写入会被拒绝。",
            {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "只读 SQL 语句，例如：SELECT name FROM sqlite_master WHERE type='table'",
                    }
                },
                "required": ["sql"],
            },
        )(query_database)
        return registry

    # ---------- 主流程 ----------

    def run(
        self,
        task: str,
        history: list[dict[str, str]] | None = None,
        kb_id: str | None = None,
    ) -> AgentResult:
        """执行一次任务，返回最终回答、引用来源与执行轨迹。"""
        steps: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        answer = ""
        for event in self.run_stream(task, history=history, kb_id=kb_id):
            event_type = event["type"]
            if event_type == "step":
                steps.append(event["step"])
            elif event_type == "tool_end":
                sources.extend(event.get("sources") or [])
            elif event_type == "token":
                answer += event["text"]
            elif event_type == "done":
                answer = event["answer"]
        return AgentResult(answer=answer, sources=sources, steps=steps)

    def run_stream(
        self,
        task: str,
        history: list[dict[str, str]] | None = None,
        kb_id: str | None = None,
    ) -> Iterable[dict[str, Any]]:
        """事件流：tool_start / tool_end / step / token / done / error。

        每一轮直接调用流式接口（支持 Function Calling）：回答内容边到边
        以 ``token`` 事件输出，工具调用流结束后统一回填执行。
        """
        if not task.strip():
            yield {"type": "done", "answer": "请输入有效问题。"}
            return

        messages = self._build_messages(task, history)
        registry = self._build_registry(kb_id)
        sources: list[dict[str, Any]] = []

        for step_no in range(1, self.max_steps + 1):
            try:
                events = self.llm.stream_chat_raw(
                    messages,
                    tools=registry.specs(),
                    temperature=0.2,
                    enable_thinking=False,
                )
                tool_calls: list[dict[str, Any]] = []
                answer_text = ""
                for event_type, payload in events:
                    if event_type == "tool_call":
                        tool_calls.append(payload)
                    elif event_type == "token":
                        answer_text += payload
                        yield {"type": "token", "text": payload}
            except Exception as exc:
                error_text = f"模型调用失败：{safe_error(exc)}"
                yield {"type": "error", "message": error_text}
                yield {"type": "done", "answer": error_text}
                return

            if not tool_calls:
                answer = answer_text.strip() or "模型未返回回答。"
                self._record(
                    step=step_no,
                    thought_summary="模型未调用工具，直接生成最终回答",
                    tool=None,
                    args={},
                    observation=answer,
                    prompt=task,
                )
                yield {"type": "done", "answer": answer}
                return

            messages.append(_assistant_message_from_tool_calls(tool_calls))
            for call in tool_calls:
                name = call.get("function", {}).get("name", "")
                arguments = self._parse_arguments(
                    call.get("function", {}).get("arguments")
                )
                yield {"type": "tool_start", "tool": name, "args": arguments}
                result = registry.call(name, arguments)
                observation = result.content[:TOOL_OBSERVATION_MAX_CHARS]
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id") or "",
                        "content": observation,
                    }
                )
                step = {
                    "step": step_no,
                    "thought_summary": f"调用 {name} 获取信息",
                    "tool": name,
                    "args": arguments,
                    "observation": observation,
                    "cost_estimate": estimate_tokens(task, observation),
                }
                sources.extend(result.sources)
                self._record(
                    step=step["step"],
                    thought_summary=step["thought_summary"],
                    tool=step["tool"],
                    args=step["args"],
                    observation=step["observation"],
                    prompt=task,
                )
                yield {"type": "step", "step": step}
                yield {
                    "type": "tool_end",
                    "tool": name,
                    "observation": observation,
                    "sources": result.sources,
                    "extra": result.extra,
                }

        message = f"已达到最大工具调用轮数（{self.max_steps} 轮），任务未完成。"
        yield {"type": "done", "answer": message}

    # ---------- 内部工具实现 ----------

    def _weather(self, city: str) -> str:
        try:
            geocode_url = (
                f"{OPEN_METEO_GEOCODING_URL}?"
                + urlencode(
                    {
                        "name": city,
                        "count": 1,
                        "language": "zh",
                        "format": "json",
                    }
                )
            )
            geo = self._get_json(geocode_url)
            results = geo.get("results") or []
            if not results:
                return f"没有找到地点“{city}”，请改用明确的城市名，例如“郑州”。"
            place = results[0]
            forecast_url = (
                f"{OPEN_METEO_FORECAST_URL}?"
                + urlencode(
                    {
                        "latitude": place["latitude"],
                        "longitude": place["longitude"],
                        "current": (
                            "temperature_2m,apparent_temperature,"
                            "relative_humidity_2m,precipitation,"
                            "weather_code,wind_speed_10m"
                        ),
                        "daily": (
                            "temperature_2m_max,temperature_2m_min,"
                            "precipitation_probability_max"
                        ),
                        "timezone": "auto",
                        "forecast_days": 1,
                    }
                )
            )
            weather = self._get_json(forecast_url)
            current = weather.get("current", {})
            daily = weather.get("daily", {})
            code = int(current.get("weather_code", -1))
            place_name = "，".join(
                str(value)
                for value in (place.get("country"), place.get("admin1"), place.get("name"))
                if value
            )
            return (
                f"**{place_name}当前天气**（{current.get('time', '时间未知')}）\n\n"
                f"- 天气：{WEATHER_CODES.get(code, f'天气代码 {code}')}\n"
                f"- 气温：{current.get('temperature_2m', '?')}°C，"
                f"体感 {current.get('apparent_temperature', '?')}°C\n"
                f"- 相对湿度：{current.get('relative_humidity_2m', '?')}%\n"
                f"- 降水量：{current.get('precipitation', '?')} mm\n"
                f"- 风速：{current.get('wind_speed_10m', '?')} km/h\n"
                f"- 今日最高/最低：{(daily.get('temperature_2m_max') or ['?'])[0]}°C / "
                f"{(daily.get('temperature_2m_min') or ['?'])[0]}°C\n"
                f"- 今日最大降水概率："
                f"{(daily.get('precipitation_probability_max') or ['?'])[0]}%\n\n"
                "数据来源：Open-Meteo。天气预报可能变化，请以当地气象部门发布为准。"
            )
        except Exception as exc:
            return f"天气查询暂时不可用：{safe_error(exc)}"

    @staticmethod
    def _get_json(url: str) -> dict[str, Any]:
        request = Request(url, headers={"User-Agent": "RAG-ReAct-PhaseA/1.0"})
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))

    def _query_database(self, sql: str) -> str:
        """以只读模式查询本地 SQLite（uri mode=ro），写入语句会被数据库直接拒绝。

        首关键字软校验 + 只读连接双重保护，避免 Agent 生成的 SQL 意外修改数据。
        """
        db_path = self.rag.config.db_path
        if not db_path.exists():
            return "数据库文件不存在。"
        lowered = (sql or "").lstrip().lower()
        if not lowered.startswith(("select", "pragma", "explain", "with")):
            return "仅允许 SELECT / PRAGMA / EXPLAIN / WITH 只读查询。"
        try:
            import sqlite3

            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(sql).fetchmany(50)
                if not rows:
                    return "查询完成：0 行。"
                header = ", ".join(rows[0].keys())
                lines = [header]
                lines.extend(", ".join(str(value) for value in row) for row in rows)
                return "\n".join(lines)[:TOOL_OBSERVATION_MAX_CHARS]
            finally:
                conn.close()
        except Exception as exc:
            return f"SQL 执行失败：{safe_error(exc)}"

    # ---------- 辅助 ----------

    def _build_messages(
        self,
        task: str,
        history: list[dict[str, str]] | None,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.agent_system_prompt}
        ]
        recent = truncate_history(history, self.rag.config.history_max_tokens)[-10:]
        messages.extend(
            {
                "role": item.get("role", "user"),
                "content": item.get("content", ""),
            }
            for item in recent
        )
        messages.append({"role": "user", "content": task})
        return messages

    @staticmethod
    def _parse_arguments(raw: str | None) -> dict[str, Any]:
        """解析模型返回的工具参数 JSON；容忍 markdown 代码块包裹。"""
        if not raw:
            return {}
        text = raw.strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _record(
        self,
        *,
        step: int,
        thought_summary: str,
        tool: str | None,
        args: dict[str, Any],
        observation: str,
        prompt: str,
    ) -> None:
        record: dict[str, Any] = {
            "step": step,
            "thought_summary": thought_summary,
            "tool": tool,
            "args": args,
            "observation": observation,
            "cost_estimate": estimate_tokens(prompt, observation),
        }
        self.logger.log(**record)


def _assistant_message_from_tool_calls(
    tool_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    """把流式聚合出的工具调用列表转成可回填 API 的 assistant 消息。"""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call.get("id") or f"call_{index}",
                "type": "function",
                "function": {
                    "name": call.get("function", {}).get("name", ""),
                    "arguments": call.get("function", {}).get("arguments", ""),
                },
            }
            for index, call in enumerate(tool_calls)
        ],
    }


def main() -> None:
    agent = ReActAgent()
    print(f"可用工具：{', '.join(agent._build_registry(None).names())}")
    print("通用文档 Agent（输入 exit 退出）")
    while True:
        try:
            task = input("\n任务> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if task.lower() in {"exit", "quit"}:
            break
        result = agent.run(task)
        print("\n" + result.answer)
        for step in result.steps:
            print(f"  [step {step['step']}] {step['tool']} <- {step['args']}")


if __name__ == "__main__":
    main()
