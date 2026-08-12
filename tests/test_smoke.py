"""端到端冒烟测试：离线跑通 上传 → 入库 → 检索 → 问答 → 删除 全链路。"""

from __future__ import annotations

import json

from rag_pipeline import RAGPipeline
from react_agent import ReActAgent
from tests.helpers import FakeLLMClient, FakeMessage, FakeToolCall, make_config, upload_text


def test_full_pipeline_offline(tmp_path) -> None:
    llm = FakeLLMClient(chat_answer="公司文档中提到：产品为道岔综合监测系统。")
    pipeline = RAGPipeline(config=make_config(tmp_path), llm_client=llm)

    kb = pipeline.create_kb("公司知识库", "产品与技术资料")
    upload_text(pipeline, kb.id, "产品手册.txt", "公司产品为道岔综合监测系统，用于铁路信号设备状态监测。" * 8, category="产品", tags="道岔,监测")
    upload_text(pipeline, kb.id, "制度文件.txt", "出差报销需提前申请并经部门负责人审批。" * 8, category="制度", tags="报销")

    result = pipeline.ingest(kb.id)
    assert result["indexed"] == ["产品手册.txt", "制度文件.txt"]
    assert pipeline.kb_stats(kb.id)["chunks"] > 0

    chunks = pipeline.retrieve(kb.id, "道岔综合监测系统是什么")
    assert chunks, "应检索到产品相关片段"
    assert "产品手册" in chunks[0].metadata["source"]

    answer = pipeline.answer("公司的产品是什么？", kb_id=kb.id)
    assert answer["sources"]
    assert answer["answer"]

    # Agent 全链路（工具调用 + 最终回答）
    agent = ReActAgent(rag=pipeline, llm_client=llm, max_steps=3)
    agent.llm.script = [
        FakeMessage(tool_calls=[FakeToolCall("search_knowledge_base", json.dumps({"query": "产品是什么"}))]),
        FakeMessage(content="产品为道岔综合监测系统。"),
    ]
    agent_result = agent.run("公司的产品是什么？", kb_id=kb.id)
    assert "道岔综合监测系统" in agent_result.answer
    assert agent_result.sources

    # 删除文档后向量同步清理
    doc = pipeline.list_documents(kb.id)[0]
    pipeline.delete_document(doc.id)
    assert pipeline.kb_stats(kb.id)["documents"] == 1
