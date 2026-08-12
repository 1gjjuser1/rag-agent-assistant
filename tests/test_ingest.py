"""入库链路测试：增量索引、文档版本、配置版本触发全量重建、旧文件迁移。"""

from __future__ import annotations

from rag_pipeline import INDEX_CONFIG_KEY, RAGPipeline
from tests.helpers import FakeLLMClient, ingest_all, make_config, upload_text


def test_ingest_and_reingest_noop(pipeline: RAGPipeline) -> None:
    kb = pipeline.create_kb("入库库")
    upload_text(pipeline, kb.id, "文档.txt", "道岔监测系统用于铁路信号设备报警。" * 10)

    first = ingest_all(pipeline, kb.id)
    assert len(first["indexed"]) == 1
    assert first["chunks"] > 0
    assert pipeline.store.count_chunks(kb.id) == first["chunks"]
    assert pipeline.vectors.count(pipeline.collection_name(kb.id)) == first["chunks"]

    second = ingest_all(pipeline, kb.id)
    assert second["indexed"] == []
    assert "已是最新" in second["message"]


def test_file_change_triggers_reindex(pipeline: RAGPipeline) -> None:
    kb = pipeline.create_kb("变更库")
    upload_text(pipeline, kb.id, "v.txt", "第一版内容。" * 10)
    ingest_all(pipeline, kb.id)
    before = pipeline.store.get_document(pipeline.list_documents(kb.id)[0].id)
    assert before is not None and before.latest_version == 1

    upload_text(pipeline, kb.id, "v.txt", "第二版内容完全不同。" * 10)
    result = ingest_all(pipeline, kb.id)
    assert result["indexed"] == ["v.txt"]
    after = pipeline.store.get_document(pipeline.list_documents(kb.id)[0].id)
    assert after is not None and after.latest_version == 2
    assert pipeline.store.count_chunks(kb.id) == after.chunk_count


def test_config_change_forces_reindex(pipeline: RAGPipeline) -> None:
    kb = pipeline.create_kb("重建库")
    upload_text(pipeline, kb.id, "r.txt", "重建测试内容。" * 10)
    ingest_all(pipeline, kb.id)
    assert pipeline.store.get_meta(INDEX_CONFIG_KEY) == pipeline._current_index_config()

    pipeline.store.set_meta(INDEX_CONFIG_KEY, "stale-config")
    result = ingest_all(pipeline, kb.id)
    assert result["force_reindex"] is True
    assert result["indexed"] == ["r.txt"]
    assert pipeline.store.get_meta(INDEX_CONFIG_KEY) == pipeline._current_index_config()


def test_delete_document_removes_vectors(pipeline: RAGPipeline) -> None:
    kb = pipeline.create_kb("删除库")
    upload_text(pipeline, kb.id, "d.txt", "删除测试内容。" * 10)
    ingest_all(pipeline, kb.id)
    doc = pipeline.list_documents(kb.id)[0]
    assert pipeline.vectors.count(pipeline.collection_name(kb.id)) > 0

    assert pipeline.delete_document(doc.id) is True
    assert pipeline.vectors.count(pipeline.collection_name(kb.id)) == 0
    assert pipeline.store.count_chunks(kb.id) == 0


def test_legacy_files_migrate_to_default_kb(tmp_path) -> None:
    docs_dir = tmp_path / "data" / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "旧文档.txt").write_text("历史遗留文档内容。" * 10, encoding="utf-8")

    pipeline = RAGPipeline(config=make_config(tmp_path), llm_client=FakeLLMClient())
    kbs = pipeline.list_kbs()
    assert len(kbs) == 1
    assert kbs[0].id == "default"
    docs = pipeline.list_documents(kbs[0].id)
    assert [doc.filename for doc in docs] == ["旧文档.txt"]
