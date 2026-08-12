"""Chroma 向量存储封装。

每个知识库对应一个 Chroma collection（命名 ``kb_<kb_id>``），向量空间固定为
余弦距离。封装提供了批量入库、按条件删除、相似度查询、取回向量四个能力，
上层（RAGPipeline）不直接接触 Chroma API。

为什么直接使用 Chroma 而不是 LangChain 封装：阶段 A 需要精确控制批量
embedding、按 ``doc_id`` 删除、取回原始向量做 MMR，直接使用原生客户端更清晰，
也便于理解每一步在做什么。
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import chromadb


class EmbeddingFunction(Protocol):
    """兼容 LangChain Embeddings 接口的最小协议。"""

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


@dataclass
class VectorHit:
    id: str
    text: str
    metadata: dict[str, Any]
    score: float  # 余弦相似度，越大越相关


class VectorStore:
    def __init__(
        self,
        chroma_dir: str | Path,
        embedding_fn: EmbeddingFunction,
        batch_size: int = 32,
    ) -> None:
        self._chroma_dir = str(chroma_dir)
        self._embedding_fn = embedding_fn
        self.batch_size = batch_size
        self._client = chromadb.PersistentClient(path=self._chroma_dir)

    def _collection(self, name: str):
        """cosine 空间：distance = 1 - 余弦相似度，相似度 = 1 - distance。"""
        return self._client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"},
        )

    def reset(self) -> None:
        """重建客户端连接（后台入库线程完成后调用，避免内存索引过期）。"""
        self._client = chromadb.PersistentClient(path=self._chroma_dir)

    def upsert(
        self,
        collection: str,
        ids: list[str],
        texts: list[str],
        metadatas: list[dict[str, Any]],
    ) -> None:
        """分批向量化并写入。id 相同则覆盖，天然支持增量更新。"""
        col = self._collection(collection)
        for start in range(0, len(texts), self.batch_size):
            end = start + self.batch_size
            embeddings = self._embedding_fn.embed_documents(texts[start:end])
            col.upsert(
                ids=ids[start:end],
                documents=texts[start:end],
                metadatas=metadatas[start:end],
                embeddings=embeddings,
            )

    def delete(
        self,
        collection: str,
        where: dict[str, Any] | None = None,
        ids: list[str] | None = None,
    ) -> None:
        self._collection(collection).delete(where=where, ids=ids)

    def list_ids(
        self,
        collection: str,
        where: dict[str, Any] | None = None,
    ) -> list[str]:
        """返回集合中满足条件的 id（用于入库时计算需清理的旧片段）。"""
        col = self._collection(collection)
        result = col.get(where=where, include=["metadatas"])
        if not result or not result.get("ids"):
            return []
        return list(result["ids"])

    def query(
        self,
        collection: str,
        query_text: str,
        k: int,
        where: dict[str, Any] | None = None,
    ) -> list[VectorHit]:
        col = self._collection(collection)
        query_vector = self._embedding_fn.embed_query(query_text)
        result = col.query(
            query_embeddings=[query_vector],
            n_results=k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        hits: list[VectorHit] = []
        if not result or not result.get("ids") or not result["ids"][0]:
            return hits
        for cid, text, meta, distance in zip(
            result["ids"][0],
            result["documents"][0],
            result["metadatas"][0],
            result["distances"][0],
            strict=False,
        ):
            hits.append(VectorHit(id=cid, text=text, metadata=meta, score=1.0 - float(distance)))
        return hits

    def get_embeddings(self, collection: str, ids: list[str]) -> dict[str, list[float]]:
        col = self._collection(collection)
        result = col.get(ids=ids, include=["embeddings"])
        if not result or not result.get("ids"):
            return {}
        return {cid: emb for cid, emb in zip(result["ids"], result["embeddings"], strict=False)}

    def query_embedding(self, text: str) -> list[float]:
        """把查询文本向量化（MMR 重排时与候选向量计算相似度）。"""
        return self._embedding_fn.embed_query(text)

    def count(self, collection: str) -> int:
        return self._collection(collection).count()

    def clear_collection(self, collection: str) -> None:
        """删除整个 collection（清空知识库向量时使用）。"""
        with contextlib.suppress(Exception):
            self._client.delete_collection(name=collection)

    def list_collections(self) -> list[str]:
        return [col.name for col in self._client.list_collections()]


class _MockEmbedding:
    """离线学习用的模拟嵌入函数：把文本哈希后生成一个稳定的伪向量。

    不依赖外部 API，便于在没有 DashScope 密钥时理解 VectorStore 的行为。
    实际相似度排序不具备语义意义，仅用于验证调用链路是否正确。
    """

    def __init__(self, dim: int = 16) -> None:
        self.dim = dim

    def _embed(self, text: str) -> list[float]:
        vec: list[float] = []
        seed = 0
        for ch in text:
            seed = (seed * 131 + ord(ch)) & 0xFFFFFFFF
        for _ in range(self.dim):
            seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
            vec.append((seed % 10000) / 10000.0 - 0.5)
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / norm for v in vec]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


def _build_embedding_fn():
    """优先使用 DashScope 真实嵌入；未配置密钥时回退到模拟嵌入。"""
    try:
        from llm_client import DashScopeClient

        client = DashScopeClient()
        return client.embeddings(), f"DashScopeEmbeddings(model={client.embedding_model})"
    except Exception as exc:
        print(f"[提示] 未加载 DashScope 嵌入，使用模拟向量进行演示：{exc}")
        return _MockEmbedding(dim=16), "MockEmbedding(dim=16, 无语义相似度)"


if __name__ == "__main__":
    import tempfile

    chroma_dir = tempfile.mkdtemp(prefix="vector_store_demo_")
    embedding_fn, embed_desc = _build_embedding_fn()

    print("=" * 60)
    print("VectorStore 学习与自测示例")
    print("=" * 60)
    print(f"Chroma 目录: {chroma_dir}")
    print(f"嵌入函数  : {embed_desc}")
    print()

    store = VectorStore(chroma_dir=chroma_dir, embedding_fn=embedding_fn, batch_size=4)
    collection = "kb_demo"

    sample_texts = [
        "Python 是一种广泛使用的高级编程语言，支持多种编程范式。",
        "LangChain 是构建 LLM 应用的流行框架，提供了 RAG 等组件。",
        "Chroma 是一个轻量级的向量数据库，用于存储和检索嵌入向量。",
        "RAG 的全称是 Retrieval-Augmented Generation，即检索增强生成。",
        "向量检索通常使用余弦相似度来衡量查询与候选文档的相关性。",
    ]
    sample_ids = [f"doc_{i}" for i in range(len(sample_texts))]
    sample_metas = [{"source": f"text_{i}.txt", "category": "技术"} for i in range(len(sample_texts))]

    print("[1/6] 批量写入 (upsert) 5 条文档片段 ...")
    store.upsert(collection, ids=sample_ids, texts=sample_texts, metadatas=sample_metas)
    print(f"      写入完成，count = {store.count(collection)}")
    print()

    print("[2/6] 列出所有 collection ...")
    print(f"      collections = {store.list_collections()}")
    print()

    query = "什么是 RAG？"
    k = 3
    print(f"[3/6] 相似度检索 (query='{query}', k={k}) ...")
    hits = store.query(collection, query_text=query, k=k)
    for idx, hit in enumerate(hits, 1):
        print(f"      [{idx}] id={hit.id}  score={hit.score:.4f}  meta={hit.metadata}")
        print(f"           text={hit.text[:40]}...")
    print()

    print("[4/6] 取回向量 (get_embeddings) 并打印维度 ...")
    vec_map = store.get_embeddings(collection, ids=[sample_ids[0], sample_ids[2]])
    for cid, vec in vec_map.items():
        print(f"      {cid}: dim={len(vec)}, 前 4 维 = {[round(v, 4) for v in vec[:4]]}")
    print()

    print("[5/6] 按 id 删除 doc_2，再查询 count ...")
    store.delete(collection, ids=[sample_ids[1]])
    print(f"      删除后 count = {store.count(collection)}")
    hits_after_del = store.query(collection, query_text=query, k=10)
    print(f"      检索结果中是否还包含 doc_1: {'doc_1' in [h.id for h in hits_after_del]}")
    print()

    print("[6/6] 清空整个 collection 并再次 count ...")
    store.clear_collection(collection)
    store.reset()
    remaining = store.list_collections()
    print(f"      剩余 collections = {remaining}, count('{collection}') 将重建空集 = {store.count(collection)}")
    print()
    print("演示完成。你可以把以上步骤对照 VectorStore 各方法源码进行学习。")
