"""RAG 管线：知识库管理、增量入库、混合检索、查询改写与带引用问答。

阶段 A 相对原版的改进：

1. **多知识库**：每个知识库独立向量 collection 与 BM25 索引，可独立增删；
2. **混合检索**：向量语义检索 + BM25 词面检索，RRF 融合，可选 MMR 去重，
   并带相关性阈值，低相关查询不再硬凑答案；
3. **增量入库**：SHA-256 判重；索引配置（切分参数/Embedding 模型）变化时
   自动全量重建，避免旧向量与新配置不匹配；
4. **文档版本**：同名文件重传自动升版本，旧版本物理文件保留；
5. **可配置**：所有参数来自 :class:`AppConfig`，无需改代码调参。

数据流：``upload_document`` 写文件 → ``ingest`` 解析/切分/向量化 →
``retrieve`` 混合检索 → ``answer`` 组装上下文并生成带引用回答。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from config import AppConfig
from ingestion import (
    SUPPORTED_SUFFIXES,
    DocumentParser,
    build_chunk_metadata,
    split_documents,
)
from llm_client import DashScopeClient, safe_error
from store import Chunk, DocumentInfo, DocumentStore, Kb
from utils.logger import estimate_tokens
from utils.retrieval import BM25Index, mmr_rerank, reciprocal_rank_fusion
from vector_store import VectorStore

INDEX_CONFIG_KEY = "index_config_version"

RAG_SYSTEM_PROMPT = """你是严谨的智能文档助手。
回答必须严格基于“检索资料”，并逐条标注引用来源。必须遵守以下规则：
1. 检索资料中没有提到的具体事实（学校、学历、日期、数字、经历、人名等），
   必须明确回答“资料中没有提到”，严禁使用模型自身知识补充或猜测；
2. 每个关键结论后标注 [来源: 文件名, 页码/段落]；无法标注来源的信息不得写入回答；
3. 如果检索资料与问题无关或资料不足，直接说明缺少哪些信息，不要编造。
如果用户要求评价候选人、简历、合同或方案，必须先概括文档中的事实，再给出有条件的专业判断；
清楚区分“文档事实”和“你的建议”，不得把建议说成文档结论。
回答应准确、简洁、结构清晰。页码按资料中的 page（从1开始）显示；无页码时显示段落编号。"""

# 简历/人物类文档常见后缀：查询中出现短名（如“李明”）时，把检索范围限定到该文档，
# 避免其他文档（如汇报、制度）污染答案、诱发幻觉。
SOURCE_HINT_SUFFIXES = ("个人简历", "简历", "简介", "履历")

RAG_USER_PROMPT = """最近对话：
{history}

用户问题：{question}

检索资料：
{context}

请给出答案并保留上述资料中的来源标识。"""

CHAT_SYSTEM_PROMPT = """你是一个可靠、友好的通用 AI 助手。
当前没有可用的用户知识库，请直接基于通用知识回答。
不要声称已经读取用户文件；对时效性信息要提示可能需要联网工具。"""

QUERY_REWRITE_PROMPT = """把用户在多轮对话中的提问改写成独立、明确的单轮问题。
只输出改写后的问题本身，不要输出任何其他内容。

最近对话：
{history}

用户问题：
{question}
"""


class LLMClient(Protocol):
    """RAG / Agent 依赖的最小 LLM 接口（便于测试注入 Fake 或替换模型厂商）。

    :class:`DashScopeClient`、测试用 ``FakeLLMClient`` 均按此协议实现。
    """

    chat_model: str
    embedding_model: str

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
        enable_search: bool = False,
        enable_thinking: bool | None = None,
    ) -> str: ...

    def chat_raw(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
        enable_search: bool = False,
        enable_thinking: bool | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = "auto",
    ) -> Any: ...

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
        enable_search: bool = False,
    ) -> Iterator[tuple[str, Any]]: ...

    def stream_chat_raw(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
        enable_search: bool = False,
        enable_thinking: bool | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = "auto",
    ) -> Iterator[tuple[str, Any]]: ...

    def web_search(self, query: str) -> str: ...

    def embeddings(self) -> Any: ...


@dataclass
class RetrievedChunk:
    """一次检索命中的片段，附带三种得分便于观察检索效果。"""

    chunk_id: str
    doc_id: str
    content: str
    metadata: dict[str, Any]
    vector_score: float  # 向量余弦相似度
    bm25_score: float  # BM25 原始得分
    rrf_score: float  # RRF 融合得分


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def truncate_history(
    history: list[dict[str, str]] | None,
    max_tokens: int,
) -> list[dict[str, str]]:
    """按 token 预算从后往前保留历史消息（至少保留最后一条）。

    长对话会稀释模型注意力（lost-in-the-middle），并且白白增加计费 token；
    与其把所有历史都塞进去，不如只保留“最近且放得下”的部分。
    """
    if not history:
        return []
    selected: list[dict[str, str]] = []
    total = 0
    for item in reversed(history):
        cost = estimate_tokens(item.get("content", ""))
        if selected and total + cost > max_tokens:
            break
        selected.append(item)
        total += cost
    return list(reversed(selected))


def fit_context_indices(blocks: list[str], max_tokens: int) -> list[int]:
    """按 token 预算保留上下文块的下标；块不可拆分，至少保留第一块。"""
    if not blocks:
        return []
    kept: list[int] = []
    total = 0
    for index, block in enumerate(blocks):
        cost = estimate_tokens(block)
        if kept and total + cost > max_tokens:
            break
        kept.append(index)
        total += cost
    return kept


class RAGPipeline:
    """RAG 管线门面：知识库管理 + 入库 + 检索 + 问答。"""

    def __init__(
        self,
        config: AppConfig | None = None,
        llm_client: LLMClient | None = None,
        embedding_fn: Any | None = None,
        parser: DocumentParser | None = None,
    ) -> None:
        self.config = config or AppConfig.from_env()
        self.llm = llm_client or DashScopeClient()
        self.store = DocumentStore(self.config.db_path)
        self.parser = parser or DocumentParser()
        self.vectors = VectorStore(
            self.config.chroma_dir,
            embedding_fn or self.llm.embeddings(),
            batch_size=self.config.embedding_batch_size,
        )
        self._bm25: dict[str, BM25Index] = {}
        self._bm25_stale: set[str] = set()
        self._chunks: dict[str, dict[str, Chunk]] = {}
        self._chunks_stale: set[str] = set()
        self._migrate_legacy_files()

    # ---------- 知识库管理 ----------

    def create_kb(self, name: str, description: str = "") -> Kb:
        return self.store.create_kb(name, description)

    def list_kbs(self) -> list[Kb]:
        return self.store.list_kbs()

    def get_kb(self, kb_id: str) -> Kb | None:
        return self.store.get_kb(kb_id)

    def get_or_create_default_kb(self) -> Kb:
        """默认知识库使用固定 id ``default``，便于命令行/旧数据兼容。"""
        kb = self.store.get_kb("default")
        if kb is None:
            kb = self.store.create_kb("默认知识库", "系统默认知识库", kb_id="default")
        return kb

    def delete_kb(self, kb_id: str) -> None:
        """删除知识库：元数据 + 向量 collection + 物理文件目录。"""
        self.store.delete_kb(kb_id)
        self.vectors.clear_collection(self.collection_name(kb_id))
        shutil.rmtree(self._kb_docs_dir(kb_id), ignore_errors=True)
        self._bm25.pop(kb_id, None)
        self._bm25_stale.discard(kb_id)
        self._chunks.pop(kb_id, None)
        self._chunks_stale.discard(kb_id)

    def kb_stats(self, kb_id: str) -> dict[str, Any]:
        docs = self.store.list_documents(kb_id)
        return {
            "kb_id": kb_id,
            "documents": len(docs),
            "chunks": sum(doc.chunk_count for doc in docs),
            "vector_count": self.vectors.count(self.collection_name(kb_id)),
        }

    # ---------- 文档管理 ----------

    def upload_document(
        self,
        kb_id: str,
        filename: str,
        data: bytes,
        category: str = "",
        tags: str = "",
    ) -> DocumentInfo:
        """保存上传文件；同名文件自动升版本，旧版本文件保留在磁盘。"""
        filename = Path(filename).name  # 防路径穿越
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            raise ValueError(f"不支持的文件类型：{suffix}，支持：{sorted(SUPPORTED_SUFFIXES)}")
        existing = self.store.find_document_by_name(kb_id, filename)
        if existing is None:
            doc = self.store.create_document(kb_id, filename, category, tags)
            version = 1
        else:
            doc = self.store.bump_document(existing.id, category, tags)
            version = doc.latest_version

        version_dir = self._kb_docs_dir(kb_id) / doc.id / f"v{version}"
        version_dir.mkdir(parents=True, exist_ok=True)
        target = version_dir / filename
        target.write_bytes(data)
        self.store.add_version(doc.id, version, sha256_bytes(data), len(data), target)
        self._chunks_stale.add(kb_id)
        self._bm25_stale.add(kb_id)
        return self.store.get_document(doc.id)  # type: ignore[return-value]

    def delete_document(self, doc_id: str) -> bool:
        """删除文档：清向量 + 清片段记录 + 软删除主记录（文件与版本历史保留）。"""
        doc = self.store.get_document(doc_id)
        if doc is None:
            return False
        self.store.delete_document(doc_id)
        self.vectors.delete(self.collection_name(doc.kb_id), where={"doc_id": doc_id})
        self._chunks_stale.add(doc.kb_id)
        self._bm25_stale.add(doc.kb_id)
        return True

    def list_documents(self, kb_id: str) -> list[DocumentInfo]:
        return self.store.list_documents(kb_id)

    # ---------- 入库 ----------

    def ingest(
        self,
        kb_id: str,
        on_progress: Callable[[str, int, int], None] | None = None,
    ) -> dict[str, Any]:
        """增量入库：只处理新增/内容变化的文件；索引配置变化时全量重建。

        on_progress(stage, done, total) 供 UI 显示进度；total 为本次待处理文件数。
        """
        if self.store.get_kb(kb_id) is None:
            return {"error": f"知识库不存在：{kb_id}", "indexed": [], "chunks": 0}

        config_version = self._current_index_config()
        stored_version = self.store.get_meta(INDEX_CONFIG_KEY)
        force_reindex = stored_version != config_version

        to_index: list[DocumentInfo] = []
        missing: list[str] = []
        for doc in self.store.list_documents(kb_id):
            path = Path(doc.file_path)
            if not path.exists():
                missing.append(doc.filename)
                continue
            digest = sha256_file(path)
            if force_reindex or digest != doc.indexed_sha:
                to_index.append(doc)

        if not to_index:
            if force_reindex:
                self.store.set_meta(INDEX_CONFIG_KEY, config_version)
            message = "知识库已是最新。"
            if missing:
                message += f" 有 {len(missing)} 个文件缺失：{', '.join(missing[:5])}"
            if on_progress:
                on_progress("完成", 0, 0)
            return {
                "indexed": [],
                "chunks": 0,
                "force_reindex": force_reindex,
                "missing": missing,
                "message": message,
            }

        collection = self.collection_name(kb_id)
        total = len(to_index)
        indexed: list[str] = []
        errors: list[str] = []
        chunks_added = 0
        for done, doc in enumerate(to_index, start=1):
            if on_progress:
                on_progress(f"解析 {doc.filename}", done, total)
            try:
                path = Path(doc.file_path)
                digest = sha256_file(path)
                documents = self.parser.load(path)
                split_docs = split_documents(
                    documents,
                    self.config.chunk_size,
                    self.config.chunk_overlap,
                )
                ids: list[str] = []
                texts: list[str] = []
                metadatas: list[dict[str, Any]] = []
                chunks: list[Chunk] = []
                for index, source_doc in enumerate(split_docs, start=1):
                    chunk_id = f"{doc.id}:{index}"
                    metadata = build_chunk_metadata(
                        source_doc,
                        doc.filename,
                        index,
                        doc.id,
                        kb_id,
                        doc.category,
                        doc.tags,
                    )
                    chunks.append(
                        Chunk(
                            id=chunk_id,
                            doc_id=doc.id,
                            kb_id=kb_id,
                            chunk_index=index,
                            content=source_doc.page_content,
                            metadata=metadata,
                        )
                    )
                    ids.append(chunk_id)
                    texts.append(source_doc.page_content)
                    metadatas.append(metadata)

                # 先写新向量，再清理已不存在的旧片段 id：若 upsert 中途失败，
                # 旧向量仍可检索（不会出现“有片段无向量”），且 indexed_sha
                # 未更新，下次 ingest 会自动重试完成。
                old_ids = set(
                    self.vectors.list_ids(collection, where={"doc_id": doc.id})
                )
                new_ids = set(ids)
                if texts:
                    self.vectors.upsert(collection, ids, texts, metadatas)
                stale_ids = old_ids - new_ids
                if stale_ids:
                    self.vectors.delete(collection, ids=list(stale_ids))
                if chunks:
                    self.store.replace_chunks(chunks)
                else:
                    self.store.delete_chunks(doc_ids=[doc.id])
                self.store.set_chunk_count(doc.id, len(chunks))
                self.store.set_indexed_sha(doc.id, digest)
                chunks_added += len(chunks)
                indexed.append(doc.filename)
            except Exception as exc:
                errors.append(f"{doc.filename}: {safe_error(exc)}")

        self.store.set_meta(INDEX_CONFIG_KEY, config_version)
        self._bm25_stale.add(kb_id)
        self._chunks_stale.add(kb_id)

        message = f"完成：更新 {len(indexed)} 个文件，共 {chunks_added} 个片段"
        if force_reindex:
            message += "（检测到索引配置变更，已全量重建）"
        if errors:
            message += f"，失败 {len(errors)} 个：{'；'.join(errors[:3])}"
        if on_progress:
            on_progress("完成", total, total)
        return {
            "indexed": indexed,
            "errors": errors,
            "chunks": chunks_added,
            "force_reindex": force_reindex,
            "message": message,
        }

    # ---------- 混合检索 ----------

    def retrieve(
        self,
        kb_id: str,
        query: str,
        k: int | None = None,
        doc_id: str | None = None,
    ) -> list[RetrievedChunk]:
        """混合检索：向量 Top-N 与 BM25 Top-N 做 RRF 融合，可选 MMR 重排。

        相关性门槛：向量与 BM25 都没有像样命中才视为“无相关内容”。
        纯关键词查询（型号、编号、人名等）BM25 强命中时，即使向量相似度
        低于 ``config.relevance_threshold`` 也会放行，避免误杀精确匹配。

        文档限定：当问题中提到某个文档的文件名或人物短名（如“李明”）时，
        自动把检索范围限定到该文档（``doc_id`` 或内部来源提示命中时生效），
        防止无关文档片段混入上下文。
        """
        k = k or self.config.top_k
        if not query.strip():
            return []
        chunks_by_id = self._chunks_map(kb_id)
        if not chunks_by_id:
            return []

        if doc_id is None:
            doc_id = self._source_hint(query, self.store.list_documents(kb_id))
        pool = max(k, self.config.fusion_pool)
        collection = self.collection_name(kb_id)
        where = {"doc_id": doc_id} if doc_id else None
        vector_hits = self.vectors.query(collection, query, k=pool, where=where)
        vector_rank = [hit.id for hit in vector_hits]

        bm25 = self._bm25_index(kb_id)
        bm25_hits = bm25.top(query, k=pool * 2) if bm25.size else []
        if doc_id:
            bm25_hits = [
                hit
                for hit in bm25_hits
                if hit[0] in chunks_by_id and chunks_by_id[hit[0]].doc_id == doc_id
            ][:pool]
        else:
            bm25_hits = bm25_hits[:pool]
        bm25_rank = [chunk_id for chunk_id, _ in bm25_hits]

        fused = reciprocal_rank_fusion([vector_rank, bm25_rank])
        best_vector_score = max((hit.score for hit in vector_hits), default=0.0)
        if best_vector_score < self.config.relevance_threshold and not bm25_hits:
            return []

        selected = sorted(fused, key=lambda chunk_id: fused[chunk_id], reverse=True)[:pool]
        if self.config.mmr_enabled and len(selected) > 1:
            query_vector = self.vectors.query_embedding(query)
            item_vectors = self.vectors.get_embeddings(collection, selected)
            if item_vectors:
                selected = mmr_rerank(
                    query_vector,
                    item_vectors,
                    lambda_=self.config.mmr_lambda,
                    k=k,
                )
            else:
                # BM25 兜底命中的候选在向量库中无向量，无法计算 MMR，直接截取。
                selected = selected[:k]
        else:
            selected = selected[:k]

        vector_scores = {hit.id: hit.score for hit in vector_hits}
        bm25_scores = {chunk_id: score for chunk_id, score in bm25_hits}
        results: list[RetrievedChunk] = []
        for chunk_id in selected:
            chunk = chunks_by_id.get(chunk_id)
            if chunk is None:
                continue
            results.append(
                RetrievedChunk(
                    chunk_id=chunk.id,
                    doc_id=chunk.doc_id,
                    content=chunk.content,
                    metadata=chunk.metadata,
                    vector_score=vector_scores.get(chunk_id, 0.0),
                    bm25_score=bm25_scores.get(chunk_id, 0.0),
                    rrf_score=fused.get(chunk_id, 0.0),
                )
            )
        return results

    def _source_hint(
        self,
        query: str,
        docs: list[DocumentInfo],
    ) -> str | None:
        """根据问题中的文件名/人物短名，猜测用户指的是哪份文档。

        匹配优先级：
        1. 文件名（含扩展名）或其主干直接出现在问题中；
        2. 去掉简历类后缀（如“李明简历”→“李明”）后的短名出现在问题中。
        """
        lowered = query.lower()
        for doc in docs:
            filename = doc.filename.lower()
            stem = Path(filename).stem.lower()
            if filename in lowered or stem in lowered:
                return doc.id
        for doc in docs:
            stem = Path(doc.filename).stem.lower()
            for suffix in SOURCE_HINT_SUFFIXES:
                short = stem.replace(suffix, "").strip(" _-")
                if len(short) >= 2 and short in lowered:
                    return doc.id
        return None

    def _bm25_index(self, kb_id: str) -> BM25Index:
        if kb_id in self._bm25_stale or kb_id not in self._bm25:
            index = BM25Index()
            index.build((chunk.id, chunk.content) for chunk in self._chunks_map(kb_id).values())
            self._bm25[kb_id] = index
            self._bm25_stale.discard(kb_id)
        return self._bm25[kb_id]

    def _chunks_map(self, kb_id: str) -> dict[str, Chunk]:
        if kb_id in self._chunks_stale or kb_id not in self._chunks:
            self._chunks[kb_id] = {chunk.id: chunk for chunk in self.store.iter_chunks(kb_id)}
            self._chunks_stale.discard(kb_id)
        return self._chunks[kb_id]

    # ---------- 问答 ----------

    def rewrite_query(self, question: str, history: list[dict[str, str]] | None) -> str:
        """多轮对话时把追问题改写成独立问题；失败时原样返回。"""
        # 改写只看最近两轮对话：历史太长或内容与问题无关时，
        # 改写反而会被“带偏”，这是长上下文注意力发散的真实风险点。
        recent = truncate_history(history, self.config.history_max_tokens)[-4:]
        if not recent:
            return question
        history_text = "\n".join(
            f"{item.get('role', 'user')}: {item.get('content', '')}" for item in recent
        )
        prompt = QUERY_REWRITE_PROMPT.format(history=history_text, question=question)
        try:
            rewritten = self.llm.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.0,
                enable_thinking=False,
            ).strip()
            # 护栏：改写结果异常长（可能把历史也抄进来了）时，退回原问题。
            if not rewritten or len(rewritten) > 300:
                return question
            return rewritten
        except Exception:
            return question

    def answer(
        self,
        question: str,
        history: list[dict[str, str]] | None = None,
        kb_id: str | None = None,
    ) -> dict[str, Any]:
        """混合检索 Top-k 后生成带引用回答；历史仅保留最近 10 轮。"""
        try:
            kb_id = self._resolve_kb(kb_id)
            if kb_id is None:
                return self.chat(question, history)
            query = question
            if self.config.query_rewrite_enabled and history:
                query = self.rewrite_query(question, history)
            chunks = self.retrieve(kb_id, query)
            if not chunks:
                return {
                    "answer": "知识库中没有检索到足够相关的内容，请换个问法或补充资料。",
                    "sources": [],
                    "query": query,
                    "kb_id": kb_id,
                    "context_tokens": 0,
                }

            context_blocks: list[str] = []
            sources: list[dict[str, Any]] = []
            for chunk in chunks:
                cite = self.citation(chunk.metadata)
                context_blocks.append(f"[来源: {cite}]\n{chunk.content}")
                sources.append(
                    {
                        "citation": cite,
                        "content": chunk.content,
                        "metadata": chunk.metadata,
                        "vector_score": round(chunk.vector_score, 4),
                        "bm25_score": round(chunk.bm25_score, 4),
                    }
                )
            recent = truncate_history(history, self.config.history_max_tokens)
            pairs = list(zip(context_blocks, sources, strict=True))
            kept_indices = fit_context_indices(
                [block for block, _ in pairs],
                self.config.rag_context_max_tokens,
            )
            context_blocks = [pairs[index][0] for index in kept_indices]
            sources = [pairs[index][1] for index in kept_indices]
            history_text = "\n".join(
                f"{item.get('role', 'user')}: {item.get('content', '')}" for item in recent
            ) or "无"
            prompt = RAG_USER_PROMPT.format(
                history=history_text,
                question=question,
                context="\n\n".join(context_blocks),
            )
            answer_text = self.llm.chat(
                [
                    {"role": "system", "content": RAG_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                # 文档问答通常不需要深度思考，关闭可明显降低首字延迟。
                enable_thinking=False,
            )
            context_tokens = estimate_tokens(
                RAG_SYSTEM_PROMPT,
                history_text,
                "\n\n".join(context_blocks),
                question,
            )
            return {
                "answer": answer_text,
                "sources": sources,
                "query": query,
                "kb_id": kb_id,
                "context_tokens": context_tokens,
            }
        except Exception as exc:
            return {"answer": f"问答服务暂时不可用：{safe_error(exc)}", "sources": []}

    def chat(
        self,
        question: str,
        history: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """无知识库（或无需检索）时进行一次普通模型聊天。"""
        try:
            recent = truncate_history(history, self.config.history_max_tokens)
            messages = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}]
            messages.extend(
                {
                    "role": item.get("role", "user"),
                    "content": item.get("content", ""),
                }
                for item in recent
            )
            messages.append({"role": "user", "content": question})
            context_tokens = estimate_tokens(
                CHAT_SYSTEM_PROMPT,
                "\n".join(item.get("content", "") for item in recent),
                question,
            )
            return {
                "answer": self.llm.chat(messages, enable_thinking=False),
                "sources": [],
                "context_tokens": context_tokens,
            }
        except Exception as exc:
            return {"answer": f"聊天服务暂时不可用：{safe_error(exc)}", "sources": []}

    # ---------- 兼容与工具 ----------

    def has_knowledge_base(self, kb_id: str | None = None) -> bool:
        resolved = self._resolve_kb(kb_id)
        return bool(resolved and self.store.list_documents(resolved))

    def list_indexed_files(self, kb_id: str | None = None) -> list[dict[str, Any]]:
        resolved = self._resolve_kb(kb_id)
        if resolved is None:
            return []
        return [
            {
                "name": doc.filename,
                "chunks": doc.chunk_count,
                "version": doc.latest_version,
                "category": doc.category,
                "tags": doc.tags,
                "doc_id": doc.id,
            }
            for doc in self.store.list_documents(resolved)
        ]

    def refresh(self) -> None:
        """后台入库完成后调用：重建向量客户端并让 BM25/片段缓存失效。"""
        self.vectors.reset()
        for kb in self.store.list_kbs():
            self._bm25_stale.add(kb.id)
            self._chunks_stale.add(kb.id)

    def collection_name(self, kb_id: str) -> str:
        return f"kb_{kb_id}"

    def _kb_docs_dir(self, kb_id: str) -> Path:
        return self.config.data_dir / "docs" / kb_id

    def _resolve_kb(self, kb_id: str | None) -> str | None:
        if kb_id and self.store.get_kb(kb_id):
            return kb_id
        default = self.store.get_kb("default")
        return default.id if default else None

    def _current_index_config(self) -> str:
        """索引配置签名：任一参数变化都会触发全量重建。"""
        return json.dumps(
            {
                "chunk_size": self.config.chunk_size,
                "chunk_overlap": self.config.chunk_overlap,
                "embedding_model": getattr(self.llm, "embedding_model", "unknown"),
                "collection_space": "cosine",
                # 解析器/切分行为变化时递增，触发存量文档全量重建。
                "parser_version": 2,
            },
            sort_keys=True,
            ensure_ascii=False,
        )

    def _migrate_legacy_files(self) -> None:
        """首次运行时，把旧版 data/docs 根目录遗留文件导入默认知识库。"""
        if self.store.list_kbs():
            return
        legacy_dir = self.config.data_dir / "docs"
        if not legacy_dir.exists():
            return
        files = sorted(
            path
            for path in legacy_dir.iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
        )
        if not files:
            return
        kb = self.get_or_create_default_kb()
        for path in files:
            try:
                self.upload_document(kb.id, path.name, path.read_bytes())
            except Exception:
                continue

    @staticmethod
    def citation(metadata: dict[str, Any]) -> str:
        source = metadata.get("source", "未知文件")
        if metadata.get("page"):
            location = f"第{metadata['page']}页"
        else:
            location = f"段落{metadata.get('paragraph', '?')}"
        return f"{source}, {location}"


if __name__ == "__main__":
    pipeline = RAGPipeline()
    kbs = pipeline.list_kbs()
    print(f"当前知识库：{len(kbs)} 个")
    for kb in kbs:
        stats = pipeline.kb_stats(kb.id)
        print(f"- {kb.name}（{kb.id}）：{stats['documents']} 个文档，{stats['chunks']} 个片段")
