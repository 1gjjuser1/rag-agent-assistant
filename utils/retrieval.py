"""混合检索工具：中文分词、BM25 索引、RRF 融合、MMR 去重重排。

这是阶段 A 检索增强的核心模块。整体思路：

1. **向量检索**（语义）：把问题与文档片段都转成向量，找语义相近的片段；
2. **BM25 检索**（词面）：基于关键词词频/逆文档频率打分，擅长精确术语；
3. **RRF 融合**：把两份排序结果按排名加权合并，鲁棒且无需调参；
4. **MMR 重排**：在“与查询相关”和“彼此不重复”之间取平衡，避免 top-k 全是同一段话。

模块不依赖外部模型，全部为纯 Python 实现，方便学习与替换。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence

import jieba
import numpy as np
from rank_bm25 import BM25Okapi

_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")

# 兜底排序用停用词：虚词/疑问词对“词面重叠”没有信息量，只会在小语料库
# BM25 全零时造成“和”“有”等高频字误命中。
_STOPWORDS = {
    "的", "了", "和", "与", "及", "或", "是", "在", "有", "为", "对", "把",
    "被", "就", "都", "也", "而", "且", "并", "等", "个", "中", "上", "下",
    "什么", "怎么", "如何", "哪", "哪些", "吗", "呢", "啊", "吧", "这", "那",
    "其", "之", "我", "你", "他", "她", "它", "我们", "你们", "他们", "一个",
}


def tokenize(text: str) -> list[str]:
    """中英文混合分词：英文/数字按单词切分，中文用 jieba 分词，统一小写。"""
    text = text or ""
    tokens: list[str] = []
    for segment in re.split(r"([A-Za-z0-9_]+)", text):
        if not segment:
            continue
        if _ASCII_TOKEN_RE.fullmatch(segment):
            tokens.append(segment.lower())
        else:
            tokens.extend(token for token in jieba.lcut(segment) if token.strip())
    return tokens


class BM25Index:
    """基于 rank_bm25 的轻量索引，以 chunk_id 作为文档主键。

    BM25（Best Matching 25）是经典的词面相关性算法：一个词在文档中出现
    得越多越相关（TF），但出现在越多文档中说明它越普通、权重越低（IDF）。
    """

    def __init__(self) -> None:
        self._ids: list[str] = []
        self._tokenized: list[list[str]] = []
        self._bm25: BM25Okapi | None = None

    def build(self, items: Iterable[tuple[str, str]]) -> None:
        """重建索引。items 为 ``(chunk_id, text)`` 序列。"""
        pairs = [(chunk_id, text) for chunk_id, text in items]
        self._ids = [chunk_id for chunk_id, _ in pairs]
        self._tokenized = [tokenize(text) for _, text in pairs]
        self._bm25 = BM25Okapi(self._tokenized) if self._tokenized else None

    @property
    def size(self) -> int:
        return len(self._ids)

    def top(self, query: str, k: int) -> list[tuple[str, float]]:
        """返回 ``[(chunk_id, bm25_score)]``，按得分降序，0 分不返回。"""
        if not self._bm25 or not self._ids:
            return []
        query_tokens = tokenize(query)
        if not query_tokens:
            return []
        scores = self._bm25.get_scores(query_tokens)
        order = np.argsort(scores)[::-1][:k]
        hits = [(self._ids[i], float(scores[i])) for i in order if scores[i] > 0]
        if hits:
            return hits
        # BM25 得分全零时兜底：极小的语料库（1~2 篇文档）中 IDF 可能为 0，
        # 精确词面匹配反而全部得 0 分；此时退化为“查询词重叠数”排序。
        query_set = {token for token in query_tokens if token not in _STOPWORDS}
        if not query_set:
            return []
        doc_sets = [
            {token for token in tokens if token not in _STOPWORDS}
            for tokens in self._tokenized
        ]
        overlaps = [len(query_set & doc_set) for doc_set in doc_sets]
        order = np.argsort(overlaps)[::-1][:k]
        return [(self._ids[i], float(overlaps[i])) for i in order if overlaps[i] > 0]


def reciprocal_rank_fusion(ranked_lists: Sequence[Sequence[str]], k: int = 60) -> dict[str, float]:
    """RRF（Reciprocal Rank Fusion）：每个列表按排名贡献 ``1 / (k + rank)``。

    优点：不需要两个检索器的得分可比，只看排名；对个别检索器的噪声不敏感。
    """
    fused: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, item in enumerate(ranked, start=1):
            fused[item] = fused.get(item, 0.0) + 1.0 / (k + rank)
    return fused


def mmr_rerank(
    query_vector: Sequence[float],
    item_vectors: Mapping[str, Sequence[float]],
    lambda_: float = 0.7,
    k: int | None = None,
) -> list[str]:
    """MMR（Maximal Marginal Relevance）重排，返回重排后的 id 列表。

    每一轮挑选使 ``λ * 与查询相似度 - (1-λ) * 与已选结果的最大相似度``
    最大的候选；λ 越大越看重相关性，越小越看重多样性。
    """
    ids = list(item_vectors)
    if not ids:
        return []
    q = np.asarray(query_vector, dtype=float)
    vectors = np.stack([np.asarray(item_vectors[cid], dtype=float) for cid in ids])
    q_norm = np.linalg.norm(q) or 1.0
    v_norms = np.linalg.norm(vectors, axis=1)
    v_norms[v_norms == 0] = 1.0
    sim_to_query = (vectors @ q) / (v_norms * q_norm)
    pair_sims = (vectors @ vectors.T) / np.outer(v_norms, v_norms)
    np.fill_diagonal(pair_sims, -np.inf)

    selected: list[int] = []
    remaining = set(range(len(ids)))
    limit = k if k is not None else len(ids)
    while remaining and len(selected) < limit:
        best_index: int | None = None
        best_score = -np.inf
        for index in remaining:
            redundancy = max(pair_sims[index, picked] for picked in selected) if selected else 0.0
            score = lambda_ * sim_to_query[index] - (1.0 - lambda_) * redundancy
            if score > best_score:
                best_score, best_index = score, index
        if best_index is None:
            break
        selected.append(best_index)
        remaining.remove(best_index)
    return [ids[index] for index in selected]
