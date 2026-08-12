"""测试公共设施：确定性假 Embedding、可脚本化的假 LLM、临时目录配置。"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import numpy as np

from config import AppConfig

EMBEDDING_DIM = 256


class FakeEmbeddings:
    """词袋式确定性嵌入：共享关键词/中文 bigram 越多的文本，余弦相似度越高。

    不依赖网络与真实模型，用于离线验证 RAG 全链路逻辑。
    """

    _token_index: dict[str, int] = {}

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self.dim = dim

    @staticmethod
    def _tokens(text: str) -> set[str]:
        tokens: set[str] = set()
        for word in re.findall(r"[A-Za-z0-9_]+", (text or "").lower()):
            tokens.add(word)
        cjk = "".join(re.findall(r"[\u4e00-\u9fff]+", text or ""))
        if len(cjk) <= 2:
            if cjk:
                tokens.add(cjk)
        else:
            tokens.update(cjk[index : index + 2] for index in range(len(cjk) - 1))
        return tokens

    def _vector(self, text: str) -> list[float]:
        vec = np.zeros(self.dim)
        for token in self._tokens(text):
            # 全局 token→维度 注册表：不同 token 尽量映射到不同维度，避免哈希碰撞
            # 导致“无关文本”也产生高相似度。
            index = FakeEmbeddings._token_index.setdefault(token, len(FakeEmbeddings._token_index))
            vec[index] += 1.0
        norm = np.linalg.norm(vec) or 1.0
        return (vec / norm).tolist()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


class FakeToolCall:
    def __init__(self, name: str, arguments: str, call_id: str = "call_1") -> None:
        self.id = call_id
        self.function = SimpleNamespace(name=name, arguments=arguments)


class FakeMessage:
    def __init__(
        self,
        content: str | None = None,
        tool_calls: list[FakeToolCall] | None = None,
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls


class FakeLLMClient:
    """可脚本化的假 LLM：chat_raw 按 script 顺序返回，chat 返回固定回答。"""

    def __init__(
        self,
        script: list[FakeMessage] | None = None,
        chat_answer: str = "文档答案是X",
    ) -> None:
        self.script = list(script or [])
        self.chat_answer = chat_answer
        self.calls: list[dict[str, Any]] = []
        self.chat_model = "fake-chat-model"
        self.embedding_model = "fake-embedding-model"

    def chat_raw(self, messages: list[dict[str, Any]], **kwargs: Any) -> FakeMessage:
        self.calls.append({"method": "chat_raw", "messages": messages, "kwargs": kwargs})
        if self.script:
            return self.script.pop(0)
        return FakeMessage(content="默认回答")

    def stream_chat_raw(
        self,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ):
        """模拟流式接口：按脚本产出 tool_call / token 事件（与真实接口同构）。"""
        self.calls.append(
            {"method": "stream_chat_raw", "messages": messages, "kwargs": kwargs}
        )
        message = (
            self.script.pop(0)
            if self.script
            else FakeMessage(content=self.chat_answer)
        )
        for call in message.tool_calls or []:
            yield (
                "tool_call",
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                },
            )
        if message.content:
            yield "token", message.content

    def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        self.calls.append({"method": "chat", "messages": messages, "kwargs": kwargs})
        return self.chat_answer

    def stream_chat(self, messages: list[dict[str, Any]], **kwargs: Any):
        yield ("content", self.chat_answer)

    def web_search(self, query: str) -> str:
        return f"联网搜索结果：{query}"

    def embeddings(self) -> FakeEmbeddings:
        return FakeEmbeddings()


def make_config(tmp_path, **overrides: Any) -> AppConfig:
    defaults = dict(
        data_dir=tmp_path / "data",
        chroma_dir=tmp_path / "data" / "chroma",
        db_path=tmp_path / "data" / "kb.sqlite3",
        chunk_size=200,
        chunk_overlap=20,
        top_k=3,
        fusion_pool=6,
        # 假嵌入是稀疏的 n-gram 词袋，余弦相似度天然低于稠密语义向量，
        # 因此测试阈值比生产默认值(0.3)低；“不相关=零重叠=0分”的判定不变。
        relevance_threshold=0.05,
        mmr_enabled=False,
        mmr_lambda=0.7,
        query_rewrite_enabled=False,
        history_max_tokens=2000,
        rag_context_max_tokens=2500,
        agent_max_steps=3,
        embedding_batch_size=16,
    )
    defaults.update(overrides)
    return AppConfig(**defaults)


def upload_text(pipeline, kb_id: str, filename: str, text: str, **kwargs: Any):
    return pipeline.upload_document(kb_id, filename, text.encode("utf-8"), **kwargs)


def ingest_all(pipeline, kb_id: str) -> dict[str, Any]:
    return pipeline.ingest(kb_id)
