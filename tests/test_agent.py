"""Agent 测试：工具注册表、function calling 循环、参数解析、最大轮数、天气工具。"""

from __future__ import annotations

import json

from react_agent import ReActAgent, ToolRegistry, ToolResult
from tests.helpers import FakeMessage, FakeToolCall, ingest_all, upload_text


def test_registry_register_and_call() -> None:
    registry = ToolRegistry()

    @registry.register("add", "两数相加", {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}, "required": ["a", "b"]})
    def add(a: int, b: int) -> ToolResult:
        return ToolResult(content=str(a + b))

    assert registry.names() == ["add"]
    assert registry.specs()[0]["function"]["name"] == "add"
    assert registry.call("add", {"a": 1, "b": 2}).content == "3"
    assert "未知工具" in registry.call("nope", {}).content


def test_parse_arguments_tolerates_fence() -> None:
    raw = '```json\n{"query": "道岔监测"}\n```'
    assert ReActAgent._parse_arguments(raw) == {"query": "道岔监测"}
    assert ReActAgent._parse_arguments("not-json") == {}


def test_agent_tool_loop_and_sources(pipeline, agent: ReActAgent) -> None:
    kb = pipeline.create_kb("Agent库")
    upload_text(pipeline, kb.id, "产品介绍.txt", "公司主营道岔综合监测系统。" * 5)
    ingest_all(pipeline, kb.id)

    agent.llm.script = [
        FakeMessage(tool_calls=[FakeToolCall("search_knowledge_base", json.dumps({"query": "公司主营什么产品"}))]),
        FakeMessage(content="公司主营道岔综合监测系统，详见产品介绍。"),
    ]
    result = agent.run("公司主营什么产品？", kb_id=kb.id)
    assert result.answer.startswith("公司主营")
    assert result.sources, "知识库工具应返回引用来源"
    assert result.steps[0]["tool"] == "search_knowledge_base"


def test_agent_stops_at_max_steps(agent: ReActAgent) -> None:
    agent.llm.script = [
        FakeMessage(tool_calls=[FakeToolCall("search_web", json.dumps({"query": "x"}))]),
        FakeMessage(tool_calls=[FakeToolCall("search_web", json.dumps({"query": "x"}))]),
        FakeMessage(tool_calls=[FakeToolCall("search_web", json.dumps({"query": "x"}))]),
    ]
    result = agent.run("重复调用工具")
    assert "最大工具调用轮数" in result.answer
    assert len(result.steps) == agent.max_steps


def test_agent_weather_tool_offline(agent: ReActAgent, monkeypatch) -> None:
    """用 URL 桩替换真实天气 API，验证工具参数解析与结果回填。"""

    class FakeResponse:
        def __init__(self, payload: str) -> None:
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args) -> bool:
            return False

        def read(self) -> bytes:
            return self._payload.encode("utf-8")

    def fake_urlopen(request, timeout: int = 15) -> FakeResponse:
        if "search" in request.full_url:
            payload = {
                "results": [
                    {
                        "latitude": 39.9,
                        "longitude": 116.4,
                        "country": "中国",
                        "admin1": "北京市",
                        "name": "北京",
                    }
                ]
            }
        else:
            payload = {
                "current": {
                    "time": "2026-08-05T12:00",
                    "temperature_2m": 25.0,
                    "apparent_temperature": 24.0,
                    "relative_humidity_2m": 40,
                    "precipitation": 0.0,
                    "weather_code": 0,
                    "wind_speed_10m": 3.0,
                },
                "daily": {
                    "temperature_2m_max": [30.0],
                    "temperature_2m_min": [20.0],
                    "precipitation_probability_max": [10],
                },
            }
        return FakeResponse(json.dumps(payload, ensure_ascii=False))

    monkeypatch.setattr("react_agent.urlopen", fake_urlopen)
    agent.llm.script = [
        FakeMessage(tool_calls=[FakeToolCall("get_weather", json.dumps({"city": "北京"}))]),
        FakeMessage(content="北京当前晴，25°C。"),
    ]
    result = agent.run("北京天气怎么样？")
    assert "25" in result.answer
    assert result.steps[0]["tool"] == "get_weather"
    assert result.steps[0]["args"] == {"city": "北京"}


def test_run_stream_event_sequence(agent: ReActAgent) -> None:
    agent.llm.script = [
        FakeMessage(tool_calls=[FakeToolCall("search_web", json.dumps({"query": "新闻"}))]),
        FakeMessage(content="最终回答。"),
    ]
    events = list(agent.run_stream("查一下最新新闻"))
    types = [event["type"] for event in events]
    assert "tool_start" in types
    assert "tool_end" in types
    assert "token" in types
    assert types[-1] == "done"


def test_run_stream_tokens_are_true_streaming(agent: ReActAgent) -> None:
    """最终回答按流式 token 输出，而不是生成后按 6 字符切块模拟。"""
    answer = "这是一段超过六个字符的完整回答，用于验证真流式输出。"
    agent.llm.script = [FakeMessage(content=answer)]
    events = list(agent.run_stream("直接回答"))
    tokens = [event["text"] for event in events if event["type"] == "token"]
    assert "".join(tokens) == answer
    # 旧实现按 6 字符切块，必然没有任何 token 长度超过 6。
    assert any(len(token) > 6 for token in tokens)
    assert events[-1]["type"] == "done"
    assert events[-1]["answer"] == answer


def test_agent_registers_database_tool(agent: ReActAgent) -> None:
    registry = agent._build_registry(None)
    assert "query_database" in registry.names()


def test_agent_database_tool_read_only(agent: ReActAgent) -> None:
    """数据库工具以只读模式查询元数据，写入语句会被拒绝。"""
    result = agent._query_database(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    assert "kbs" in result
    write_result = agent._query_database("CREATE TABLE hack (id INTEGER)")
    assert "只读" in write_result or "失败" in write_result


def test_run_empty_task(agent: ReActAgent) -> None:
    result = agent.run("   ")
    assert "请输入有效问题" in result.answer
