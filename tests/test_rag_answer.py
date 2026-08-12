"""RAG 问答测试：引用来源、相关性阈值、查询改写、多知识库隔离。"""

from __future__ import annotations

from rag_pipeline import RAGPipeline
from tests.helpers import FakeLLMClient, ingest_all, make_config, upload_text

KB_TEXT = (
    "道岔综合监测系统部署在铁路车站，用于实时监测道岔状态。"
    "系统由传感器、采集单元、监测主机和监控软件组成。"
    "当发生报警时，值班人员需要查看报警详情并按规定上报。"
) * 3


def test_answer_with_sources(pipeline: RAGPipeline) -> None:
    kb = pipeline.create_kb("问答库")
    upload_text(pipeline, kb.id, "道岔监测汇报.txt", KB_TEXT)
    ingest_all(pipeline, kb.id)

    result = pipeline.answer("道岔监测系统有什么功能？", kb_id=kb.id)
    assert result["sources"], "应有引用来源"
    assert "道岔监测汇报.txt" in result["sources"][0]["citation"]
    assert result["answer"] == "文档答案是X"


def test_irrelevant_query_blocked_by_threshold(pipeline: RAGPipeline) -> None:
    kb = pipeline.create_kb("阈值库")
    upload_text(pipeline, kb.id, "道岔监测汇报.txt", KB_TEXT)
    ingest_all(pipeline, kb.id)

    result = pipeline.answer("量子力学和火星移民有什么关系", kb_id=kb.id)
    assert result["sources"] == []
    assert "没有检索到" in result["answer"]


def test_rewrite_query_uses_history(tmp_path) -> None:
    llm = FakeLLMClient(chat_answer="道岔监测系统有哪些组成")
    pipeline = RAGPipeline(
        config=make_config(tmp_path, query_rewrite_enabled=True),
        llm_client=llm,
    )
    history = [{"role": "user", "content": "介绍一下道岔综合监测系统"}, {"role": "assistant", "content": "好的。"}]
    rewritten = pipeline.rewrite_query("它有哪些组成？", history)
    assert rewritten == "道岔监测系统有哪些组成"
    assert llm.calls[0]["method"] == "chat"


def test_no_history_skips_rewrite(pipeline: RAGPipeline) -> None:
    assert pipeline.rewrite_query("直接提问", None) == "直接提问"


def test_multiple_kbs_isolated(pipeline: RAGPipeline) -> None:
    kb_a = pipeline.create_kb("库A")
    kb_b = pipeline.create_kb("库B")
    upload_text(pipeline, kb_a.id, "a.txt", "铁路道岔监测系统说明。" * 5)
    upload_text(pipeline, kb_b.id, "b.txt", "公司财务报销制度说明。" * 5)
    ingest_all(pipeline, kb_a.id)
    ingest_all(pipeline, kb_b.id)

    assert pipeline.store.count_chunks(kb_a.id) > 0
    assert pipeline.store.count_chunks(kb_b.id) > 0
    assert pipeline.kb_stats(kb_a.id)["documents"] == 1
    assert pipeline.kb_stats(kb_b.id)["documents"] == 1


def test_source_hint_matches_short_name(pipeline: RAGPipeline) -> None:
    kb = pipeline.create_kb("来源提示库")
    upload_text(pipeline, kb.id, "李明简历.txt", "教育背景：北京石油化工学院硕士、中原工学院本科。" * 3)
    upload_text(pipeline, kb.id, "铁路汇报.txt", "太原局铁路视频智能监测系统报警情况说明。" * 3)
    ingest_all(pipeline, kb.id)

    docs = pipeline.list_documents(kb.id)
    hint = pipeline._source_hint("李明的学历是什么？", docs)
    assert hint == next(doc.id for doc in docs if doc.filename == "李明简历.txt")

    # 文件名主干直接命中
    hint2 = pipeline._source_hint("李明简历里写了什么", docs)
    assert hint2 == hint

    # 不涉及任何文档时不做限定
    assert pipeline._source_hint("今天天气怎么样", docs) is None


def test_source_hint_restricts_retrieval(pipeline: RAGPipeline) -> None:
    kb = pipeline.create_kb("限定检索库")
    upload_text(
        pipeline,
        kb.id,
        "李明简历.txt",
        "学历：教育背景：北京石油化工学院人工智能研究院硕士，中原工学院软件工程本科。" * 2,
    )
    upload_text(
        pipeline,
        kb.id,
        "太原局汇报.txt",
        "太原局铁路视频智能监测系统报警情况，涉及太原理工大学相关试验线。" * 2,
    )
    ingest_all(pipeline, kb.id)

    chunks = pipeline.retrieve(kb.id, "李明的学历是什么")
    assert chunks, "应检索到简历片段"
    assert {chunk.metadata["source"] for chunk in chunks} == {"李明简历.txt"}
    # 简历内容被限定后，其他文档即使包含“太原理工大学”也不会进入上下文。
    assert all("太原理工大学" not in chunk.content for chunk in chunks)


def test_rag_prompt_forbids_hallucination() -> None:
    from rag_pipeline import RAG_SYSTEM_PROMPT

    assert "严禁使用模型自身知识补充或猜测" in RAG_SYSTEM_PROMPT
    assert "资料中没有提到" in RAG_SYSTEM_PROMPT
    assert "无法标注来源的信息不得写入回答" in RAG_SYSTEM_PROMPT
