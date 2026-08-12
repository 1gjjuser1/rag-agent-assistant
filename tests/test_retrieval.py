"""混合检索工具单元测试：分词、BM25、RRF、MMR。"""

from __future__ import annotations

import numpy as np

from rag_pipeline import RAGPipeline
from tests.helpers import ingest_all, upload_text
from utils.retrieval import BM25Index, mmr_rerank, reciprocal_rank_fusion, tokenize


def test_tokenize_mixed_chinese_and_ascii() -> None:
    tokens = tokenize("道岔监测系统 S7-1200 PLC")
    assert "道岔" in tokens
    assert "监测" in tokens
    assert "s7" in tokens
    assert "1200" in tokens
    assert "plc" in tokens


def test_bm25_ranks_keyword_match_first() -> None:
    index = BM25Index()
    index.build(
        [
            ("a", "道岔监测系统用于铁路信号设备报警"),
            ("b", "公司产品包括道岔综合监测系统"),
            ("c", "天气预报和城市生活"),
        ]
    )
    hits = index.top("道岔监测", k=2)
    assert hits[0][0] == "a" or hits[0][0] == "b"
    assert hits[0][1] > 0


def test_bm25_empty_index() -> None:
    index = BM25Index()
    assert index.top("任意查询", k=3) == []


def test_rrf_fuses_rankings() -> None:
    # b 在两个列表中都排第 1，融合分应最高。
    fused = reciprocal_rank_fusion([["b", "a", "c"], ["b", "d", "a"]])
    assert fused["b"] > fused["a"]
    assert fused["a"] > fused["c"]
    assert fused["a"] > fused["d"]


def test_mmr_selects_diverse_items() -> None:
    query = np.array([1.0, 0.0])
    vectors = {
        "a": [1.0, 0.1],  # 与查询最相关
        "b": [1.0, 0.5],  # 相关性次高但与 a 高度重复
        "c": [0.4, 1.0],  # 相关性较低但与 a/b 差异大
    }
    ordered = mmr_rerank(query, vectors, lambda_=0.3, k=2)
    assert ordered[0] == "a"
    assert ordered[1] == "c"  # λ 较低时 MMR 倾向多样性，选 c 而非重复的 b


def test_mmr_empty_input() -> None:
    assert mmr_rerank([1.0, 0.0], {}, k=2) == []


def test_bm25_fallback_when_vector_below_threshold(
    pipeline: RAGPipeline, monkeypatch
) -> None:
    """向量相似度低于阈值但 BM25 强命中时，检索应兜底放行而不是整体过滤。"""
    kb = pipeline.create_kb("BM25兜底库")
    upload_text(
        pipeline,
        kb.id,
        "型号清单.txt",
        "S7-1200 PLC 是道岔监测系统的核心控制器型号，支持以太网通信。" * 8,
    )
    ingest_all(pipeline, kb.id)

    monkeypatch.setattr(pipeline.vectors, "query", lambda *args, **kwargs: [])
    chunks = pipeline.retrieve(kb.id, "S7-1200 PLC")
    assert chunks, "向量无命中且低于阈值时，BM25 强命中应兜底返回结果"
    assert any("S7-1200" in chunk.content for chunk in chunks)
