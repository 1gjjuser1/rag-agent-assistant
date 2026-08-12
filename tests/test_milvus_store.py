"""MilvusStore 单元测试：本地模式（Milvus Lite，无需 Docker）全链路。"""

from __future__ import annotations

import pytest

from tests.helpers import EMBEDDING_DIM, FakeEmbeddings
from vector_store import MilvusStore


@pytest.fixture
def store(tmp_path) -> MilvusStore:
    return MilvusStore(tmp_path / "milvus.db", FakeEmbeddings(), batch_size=4)


def test_upsert_search_and_count(store: MilvusStore) -> None:
    store.upsert(
        "kb_a",
        ["a:1", "a:2"],
        ["道岔监测系统用于铁路信号设备", "公司出差报销制度说明"],
        [
            {"doc_id": "a", "source": "产品介绍.txt", "paragraph": 1},
            {"doc_id": "a", "source": "报销制度.txt", "paragraph": 2},
        ],
    )
    assert store.count("kb_a") == 2
    hits = store.query("kb_a", "道岔监测系统", k=2)
    assert len(hits) == 2
    assert hits[0].id == "a:1"
    assert hits[0].metadata["source"] == "产品介绍.txt"
    assert hits[0].score > 0


def test_upsert_overwrites_same_id(store: MilvusStore) -> None:
    store.upsert("kb_b", ["x:1"], ["内容"], [{"doc_id": "x", "paragraph": 1}])
    store.upsert("kb_b", ["x:1"], ["新内容"], [{"doc_id": "x", "paragraph": 9}])
    assert store.count("kb_b") == 1
    vectors = store.get_embeddings("kb_b", ["x:1"])
    assert len(vectors["x:1"]) == EMBEDDING_DIM


def test_filter_delete_and_list_ids(store: MilvusStore) -> None:
    store.upsert(
        "kb_c",
        ["d1:1", "d1:2", "d2:1"],
        ["a", "b", "c"],
        [
            {"doc_id": "d1", "paragraph": 1},
            {"doc_id": "d1", "paragraph": 2},
            {"doc_id": "d2", "paragraph": 1},
        ],
    )
    assert set(store.list_ids("kb_c", where={"doc_id": "d1"})) == {"d1:1", "d1:2"}
    store.delete("kb_c", where={"doc_id": "d1"})
    assert set(store.list_ids("kb_c")) == {"d2:1"}
    store.delete("kb_c", ids=["d2:1"])
    assert store.count("kb_c") == 0


def test_clear_collection(store: MilvusStore) -> None:
    store.upsert("kb_d", ["x:1"], ["内容"], [{"doc_id": "x"}])
    store.clear_collection("kb_d")
    assert store.count("kb_d") == 0
    assert "kb_d" not in store.list_collections()
