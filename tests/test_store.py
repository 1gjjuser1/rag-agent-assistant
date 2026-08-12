"""SQLite 文档库单元测试：知识库、文档版本、片段、meta。"""

from __future__ import annotations

import pytest

from store import Chunk, DocumentStore


@pytest.fixture
def store(tmp_path) -> DocumentStore:
    return DocumentStore(tmp_path / "kb.sqlite3")


def test_create_and_list_kb(store: DocumentStore) -> None:
    kb = store.create_kb("测试库", "用于测试")
    assert kb.name == "测试库"
    assert store.list_kbs()[0].id == kb.id


def test_duplicate_kb_name_raises(store: DocumentStore) -> None:
    store.create_kb("重复库")
    with pytest.raises(ValueError):
        store.create_kb("重复库")


def test_document_versioning(store: DocumentStore) -> None:
    kb = store.create_kb("版本库")
    doc = store.create_document(kb.id, "说明.txt")
    store.add_version(doc.id, 1, "hash-1", 10, "/tmp/v1/说明.txt")
    assert store.get_document(doc.id).latest_version == 1  # type: ignore[union-attr]

    bumped = store.bump_document(doc.id)
    store.add_version(bumped.id, 2, "hash-2", 20, "/tmp/v2/说明.txt")
    latest = store.get_document(doc.id)
    assert latest is not None
    assert latest.latest_version == 2
    assert latest.sha256 == "hash-2"


def test_chunks_replace_and_delete(store: DocumentStore) -> None:
    kb = store.create_kb("片段库")
    doc = store.create_document(kb.id, "a.txt")
    chunks = [
        Chunk(id=f"{doc.id}:1", doc_id=doc.id, kb_id=kb.id, chunk_index=1, content="第一段", metadata={"source": "a.txt"}),
        Chunk(id=f"{doc.id}:2", doc_id=doc.id, kb_id=kb.id, chunk_index=2, content="第二段", metadata={"source": "a.txt"}),
    ]
    store.replace_chunks(chunks)
    assert store.count_chunks(kb.id) == 2
    assert [c.content for c in store.iter_chunks(kb.id)] == ["第一段", "第二段"]

    store.delete_chunks(doc_ids=[doc.id])
    assert store.count_chunks(kb.id) == 0


def test_meta_roundtrip(store: DocumentStore) -> None:
    assert store.get_meta("index_config_version") is None
    store.set_meta("index_config_version", "v2")
    assert store.get_meta("index_config_version") == "v2"


def test_soft_delete_document(store: DocumentStore) -> None:
    kb = store.create_kb("删除库")
    doc = store.create_document(kb.id, "b.txt")
    store.add_version(doc.id, 1, "h", 5, "/tmp/b.txt")
    assert store.delete_document(doc.id) is not None
    assert store.list_documents(kb.id) == []
    assert len(store.list_documents(kb.id, include_deleted=True)) == 1
