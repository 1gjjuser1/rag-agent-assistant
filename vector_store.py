"""向量存储封装：Milvus（默认）与 Chroma（备选）双后端。

每个知识库对应一个 collection（命名 ``kb_<kb_id>``），向量空间固定为余弦。
封装提供批量入库、按条件删除、相似度查询、取回向量四个能力，上层
（RAGPipeline）不直接接触具体数据库 API。

为什么默认 Milvus：Milvus 是目前招聘与生产环境的主流向量数据库，支持标量过滤、
增量 upsert 与余弦检索；本地开发用 Milvus Lite（``pip install pymilvus`` 即可，
零部署），生产可把 ``MILVUS_URI`` 指向 Docker/集群部署的 Milvus 服务端，
同一套代码无痛切换。``VECTOR_STORE=chroma`` 可回退到原 Chroma 后端。
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import chromadb
from pymilvus import MilvusClient


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
    """Chroma 后端（备选）。"""

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


class MilvusStore:
    """Milvus 后端：本地模式（Milvus Lite 文件）或服务端，接口与 VectorStore 一致。

    - 本地模式：``uri="data/milvus.db"``，无需 Docker；
    - 服务端：``uri="http://localhost:19530"``（Milvus standalone / 集群）。

    检索语义：COSINE 指标返回的距离即余弦相似度，越大越相关，与 Chroma 后端
    的 ``score`` 含义一致（上层无需区分后端）。
    """

    _OUTPUT_FIELDS = [
        "doc_id",
        "kb_id",
        "source",
        "chunk_index",
        "category",
        "tags",
        "paragraph",
        "page",
    ]

    def __init__(
        self,
        uri: str | Path,
        embedding_fn: EmbeddingFunction,
        batch_size: int = 32,
    ) -> None:
        self._uri = str(uri)
        self._embedding_fn = embedding_fn
        self.batch_size = batch_size
        self._client = MilvusClient(uri=self._uri)

    def _collection(self, name: str) -> str:
        """集合不存在时按 embedding 维度自动创建（COSINE + string 主键）。"""
        if not self._client.has_collection(name):
            dimension = len(self._embedding_fn.embed_query("dimension probe"))
            try:
                self._client.create_collection(
                    collection_name=name,
                    dimension=dimension,
                    primary_field_name="id",
                    id_type="string",
                    metric_type="COSINE",
                    auto_id=False,
                    enable_dynamic_field=True,
                )
            except Exception:
                # 并发场景下另一个线程可能已创建，重查一次再决定是否抛出。
                if not self._client.has_collection(name):
                    raise
        return name

    def reset(self) -> None:
        """重建客户端连接（后台入库线程完成后调用，避免内存状态过期）。"""
        self._client = MilvusClient(uri=self._uri)

    def upsert(
        self,
        collection: str,
        ids: list[str],
        texts: list[str],
        metadatas: list[dict[str, Any]],
    ) -> None:
        """分批向量化并写入；id 相同则覆盖，天然支持增量更新。"""
        col = self._collection(collection)
        for start in range(0, len(texts), self.batch_size):
            end = start + self.batch_size
            embeddings = self._embedding_fn.embed_documents(texts[start:end])
            data: list[dict[str, Any]] = []
            for index, chunk_id in enumerate(ids[start:end]):
                item: dict[str, Any] = {
                    "id": chunk_id,
                    "vector": embeddings[index],
                }
                for key, value in metadatas[start:end][index].items():
                    if value is not None and isinstance(
                        value, (str, int, float, bool)
                    ):
                        item[key] = value
                data.append(item)
            self._client.upsert(col, data=data)

    def delete(
        self,
        collection: str,
        where: dict[str, Any] | None = None,
        ids: list[str] | None = None,
    ) -> None:
        if not self._client.has_collection(collection):
            return
        if ids:
            self._client.delete(collection, ids=[str(i) for i in ids])
        if where:
            self._client.delete(collection, filter=self._filter_expr(where))

    def query(
        self,
        collection: str,
        query_text: str,
        k: int,
        where: dict[str, Any] | None = None,
    ) -> list[VectorHit]:
        if not self._client.has_collection(collection):
            return []
        query_vector = self._embedding_fn.embed_query(query_text)
        expr = self._filter_expr(where) if where else None
        result = self._client.search(
            collection,
            data=[query_vector],
            limit=k,
            filter=expr,
            output_fields=self._OUTPUT_FIELDS,
        )
        hits: list[VectorHit] = []
        if not result:
            return hits
        for hit in result[0]:
            entity = hit.get("entity", {}) or {}
            metadata = {
                field: entity[field]
                for field in self._OUTPUT_FIELDS
                if entity.get(field) is not None
            }
            hits.append(
                VectorHit(
                    id=str(hit["id"]),
                    text="",  # 片段原文由 SQLite 提供，向量库不冗余保存
                    metadata=metadata,
                    score=float(hit["distance"]),  # COSINE 距离即相似度
                )
            )
        return hits

    def get_embeddings(
        self,
        collection: str,
        ids: list[str],
    ) -> dict[str, list[float]]:
        if not ids or not self._client.has_collection(collection):
            return {}
        result = self._client.get(
            collection,
            ids=[str(i) for i in ids],
            output_fields=["vector"],
        )
        return {str(row["id"]): row["vector"] for row in result}

    def query_embedding(self, text: str) -> list[float]:
        return self._embedding_fn.embed_query(text)

    def count(self, collection: str) -> int:
        if not self._client.has_collection(collection):
            return 0
        stats = self._client.get_collection_stats(collection)
        return int(stats.get("row_count", 0))

    def clear_collection(self, collection: str) -> None:
        with contextlib.suppress(Exception):
            self._client.drop_collection(collection)

    def list_collections(self) -> list[str]:
        return list(self._client.list_collections())

    def list_ids(
        self,
        collection: str,
        where: dict[str, Any] | None = None,
    ) -> list[str]:
        """返回集合中满足条件的 id（分页拉取，避免超过单次查询上限）。"""
        if not self._client.has_collection(collection):
            return []
        expr = self._filter_expr(where) if where else None
        ids: list[str] = []
        limit = 16384
        offset = 0
        while True:
            rows = self._client.query(
                collection,
                filter=expr,
                output_fields=["id"],
                limit=limit,
                offset=offset,
            )
            ids.extend(str(row["id"]) for row in rows)
            if len(rows) < limit:
                break
            offset += limit
        return ids

    @staticmethod
    def _filter_expr(where: dict[str, Any]) -> str:
        """把 ``{"doc_id": "x"}`` 转成 Milvus 过滤表达式。"""
        parts: list[str] = []
        for key, value in where.items():
            if isinstance(value, bool):
                parts.append(f"{key} == {'true' if value else 'false'}")
            elif isinstance(value, (int, float)):
                parts.append(f"{key} == {value}")
            else:
                escaped = str(value).replace('"', '\\"')
                parts.append(f'{key} == "{escaped}"')
        return " and ".join(parts)


def create_vector_store(config: Any, embedding_fn: EmbeddingFunction):
    """按配置创建向量存储后端（milvus 默认 / chroma 备选）。"""
    if getattr(config, "vector_store", "milvus") == "milvus":
        return MilvusStore(
            config.milvus_uri,
            embedding_fn,
            batch_size=config.embedding_batch_size,
        )
    return VectorStore(
        config.chroma_dir,
        embedding_fn,
        batch_size=config.embedding_batch_size,
    )


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
