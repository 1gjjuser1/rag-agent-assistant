"""百炼 OpenAI 兼容大模型与 DashScope Embedding 的统一安全封装。"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

from dotenv import load_dotenv
from langchain_community.embeddings import DashScopeEmbeddings
from openai import OpenAI

load_dotenv()

# 默认使用 DashScope 公共兼容端点与公共模型名，clone 后开箱即用；
# 私有 MaaS 业务空间 / 私有模型名请在 .env 中覆盖（见 .env.example）。
DEFAULT_CHAT_MODEL = "qwen-plus"
DEFAULT_EMBEDDING_MODEL = "text-embedding-v3"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class DashScopeClient:
    """只从环境变量读取密钥，并将外部异常转换为可读错误。"""

    def __init__(
        self,
        chat_model: str | None = None,
        embedding_model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.chat_model = (
            chat_model or os.getenv("DASHSCOPE_CHAT_MODEL") or DEFAULT_CHAT_MODEL
        ).strip()
        self.embedding_model = (
            embedding_model
            or os.getenv("DASHSCOPE_EMBEDDING_MODEL")
            or DEFAULT_EMBEDDING_MODEL
        ).strip()
        self.base_url = (
            base_url or os.getenv("DASHSCOPE_BASE_URL") or DEFAULT_BASE_URL
        ).strip().rstrip("/")
        self.enable_thinking = (
            os.getenv("DASHSCOPE_ENABLE_THINKING", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.last_usage: dict[str, int] | None = None

    def _record_usage(self, completion: Any) -> None:
        """记录最近一次调用的 token 用量（prompt/completion/total）。"""
        usage = getattr(completion, "usage", None)
        if usage is None:
            return
        self.last_usage = {
            "prompt_tokens": int(getattr(usage, "prompt_tokens", 0)),
            "completion_tokens": int(getattr(usage, "completion_tokens", 0)),
            "total_tokens": int(getattr(usage, "total_tokens", 0)),
        }

    @staticmethod
    def _api_key() -> str:
        key = os.getenv("DASHSCOPE_API_KEY", "").strip()
        if not key:
            raise RuntimeError(
                "未配置 DASHSCOPE_API_KEY。请复制 .env.example 为 .env 并填写有效密钥。"
            )
        return key

    def embeddings(self) -> DashScopeEmbeddings:
        """返回 LangChain DashScope Embedding 实例。"""
        return DashScopeEmbeddings(
            model=self.embedding_model,
            dashscope_api_key=self._api_key(),
        )

    def _openai_client(self) -> OpenAI:
        """创建百炼 OpenAI 兼容客户端，不复用全局可变连接状态。"""
        if not self.base_url.endswith("/compatible-mode/v1"):
            raise RuntimeError(
                "DASHSCOPE_BASE_URL 格式不正确，应以 /compatible-mode/v1 结尾。"
            )
        return OpenAI(
            api_key=self._api_key(),
            base_url=self.base_url,
            timeout=90.0,
            max_retries=2,
        )

    def _extra_body(self, enable_search: bool) -> dict[str, bool]:
        extra_body = {"enable_thinking": self.enable_thinking}
        if enable_search:
            extra_body["enable_search"] = True
        return extra_body

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
        enable_search: bool = False,
        enable_thinking: bool | None = None,
    ) -> str:
        """通过百炼 OpenAI 兼容接口调用模型并返回最终回答。"""
        try:
            extra_body = self._extra_body(enable_search)
            if enable_thinking is not None:
                extra_body["enable_thinking"] = enable_thinking
            completion: Any = self._openai_client().chat.completions.create(
                model=self.chat_model,
                messages=messages,  # type: ignore[arg-type]
                temperature=temperature,
                extra_body=extra_body,
                stream=False,
            )
            if not completion.choices:
                raise RuntimeError("百炼服务返回为空。")
            self._record_usage(completion)
            content = completion.choices[0].message.content
            if not content:
                raise RuntimeError("百炼服务未返回最终回答内容。")
            return content.strip()
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"百炼模型调用失败（model={self.chat_model}, "
                f"base_url={self.base_url}）：{exc}"
            ) from exc

    def chat_raw(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
        enable_search: bool = False,
        enable_thinking: bool | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = "auto",
    ) -> Any:
        """返回完整 message 对象（含 tool_calls），供 Agent 工具循环使用。

        messages 与 tools 都是 OpenAI 兼容格式；百炼兼容接口支持 Qwen 系列
        的 function calling。返回对象的 ``tool_calls`` 为 None 时表示模型
        已给出最终回答（content），否则需要 Agent 执行工具并把结果回填。
        """
        try:
            extra_body = self._extra_body(enable_search)
            if enable_thinking is not None:
                extra_body["enable_thinking"] = enable_thinking
            kwargs: dict[str, Any] = {
                "model": self.chat_model,
                "messages": messages,
                "temperature": temperature,
                "extra_body": extra_body,
                "stream": False,
            }
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = tool_choice or "auto"
            completion: Any = self._openai_client().chat.completions.create(**kwargs)
            if not completion.choices:
                raise RuntimeError("百炼服务返回为空。")
            self._record_usage(completion)
            return completion.choices[0].message
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"百炼模型调用失败（model={self.chat_model}, "
                f"base_url={self.base_url}）：{exc}"
            ) from exc

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
        enable_search: bool = False,
    ) -> Iterator[tuple[str, str]]:
        """流式输出 ``(类型, 内容)``；类型为 reasoning 或 content。"""
        try:
            completion: Any = self._openai_client().chat.completions.create(
                model=self.chat_model,
                messages=messages,  # type: ignore[arg-type]
                temperature=temperature,
                extra_body=self._extra_body(enable_search),
                stream=True,
            )
            for chunk in completion:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    yield "reasoning", reasoning
                if delta.content:
                    yield "content", delta.content
        except Exception as exc:
            raise RuntimeError(
                f"百炼流式调用失败（model={self.chat_model}, "
                f"base_url={self.base_url}）：{exc}"
            ) from exc

    def stream_chat_raw(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
        enable_search: bool = False,
        enable_thinking: bool | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = "auto",
    ) -> Iterator[tuple[str, Any]]:
        """流式调用（支持 Function Calling），产出 ``(event_type, payload)`` 事件。

        事件类型：
        - ``("token", str)``：最终回答增量，边到边 yield，UI 首字延迟低；
        - ``("tool_call", dict)``：完整工具调用（OpenAI 兼容格式），
          由于 tool_calls 以增量帧到达，先按 index 累积、流结束后统一产出。

        返回的 tool_call 结构与 :meth:`chat_raw` 的 ``message.tool_calls`` 一致，
        Agent 可直接用于回填 ``role=tool`` 消息。
        """
        try:
            extra_body = self._extra_body(enable_search)
            if enable_thinking is not None:
                extra_body["enable_thinking"] = enable_thinking
            kwargs: dict[str, Any] = {
                "model": self.chat_model,
                "messages": messages,
                "temperature": temperature,
                "extra_body": extra_body,
                "stream": True,
            }
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = tool_choice or "auto"
            completion: Any = self._openai_client().chat.completions.create(**kwargs)

            tool_calls: dict[int, dict[str, Any]] = {}
            for chunk in completion:
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    self.last_usage = {
                        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0)),
                        "completion_tokens": int(getattr(usage, "completion_tokens", 0)),
                        "total_tokens": int(getattr(usage, "total_tokens", 0)),
                    }
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta.content:
                    yield "token", delta.content
                for call in getattr(delta, "tool_calls", None) or []:
                    index = int(getattr(call, "index", 0))
                    entry = tool_calls.setdefault(
                        index,
                        {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        },
                    )
                    if getattr(call, "id", None):
                        entry["id"] = call.id
                    function = getattr(call, "function", None)
                    if function is not None:
                        if getattr(function, "name", None):
                            entry["function"]["name"] = function.name
                        if getattr(function, "arguments", None):
                            entry["function"]["arguments"] += function.arguments or ""

            for index in sorted(tool_calls):
                yield "tool_call", tool_calls[index]
        except Exception as exc:
            raise RuntimeError(
                f"百炼流式调用失败（model={self.chat_model}, "
                f"base_url={self.base_url}）：{exc}"
            ) from exc

    def complete(self, prompt: str, *, temperature: float = 0.2) -> str:
        return self.chat([{"role": "user", "content": prompt}], temperature=temperature)

    def web_search(self, query: str) -> str:
        """使用 DashScope 联网搜索；不支持时由上层 Agent 自动降级。"""
        return self.chat(
            [
                {
                    "role": "system",
                    "content": "请联网检索并简洁回答，说明信息可能随时间变化。",
                },
                {"role": "user", "content": query},
            ],
            enable_search=True,
        )


def safe_error(exc: Exception) -> str:
    """对 UI 隐藏冗长堆栈，同时保留可排障原因。"""
    return f"{type(exc).__name__}: {exc}"


if __name__ == "__main__":
    client = DashScopeClient()
    try:
        print(f"model={client.chat_model}")
        print(f"base_url={client.base_url}")
        print("\n" + "=" * 20 + "思考过程" + "=" * 20)
        is_answering = False
        for event_type, text in client.stream_chat(
            [{"role": "user", "content": "你是谁？"}]
        ):
            if event_type == "reasoning" and not is_answering:
                print(text, end="", flush=True)
            elif event_type == "content":
                if not is_answering:
                    print("\n" + "=" * 20 + "完整回复" + "=" * 20)
                    is_answering = True
                print(text, end="", flush=True)
        print()
    except Exception as error:
        print(f"连接测试未通过：{safe_error(error)}")
