"""轻量评测：golden set 检索命中率 / 回答质量 / 可溯源率。

用法（在项目根目录执行）：
    python evals/run_eval.py             # 在线：真实 DashScope Embedding + LLM 问答
    python evals/run_eval.py --offline    # 离线：确定性伪向量，只验证评测链路本身

指标说明：
    hit@k        期望来源是否出现在 Top-k 检索结果中（检索质量）；
    keyword_hit  LLM 回答是否包含预期关键词（回答质量，仅在线模式）；
    citation     回答是否附带引用来源（可溯源，仅在线模式）。

结果写入 evals/last_run_report.json，便于把指标写进简历/README。
扩展方向：接入 RAGAS（faithfulness / answer relevancy）做自动评测。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

EVALS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = EVALS_DIR.parent
SAMPLE_DOCS = EVALS_DIR / "sample_docs"
GOLDEN_SET = EVALS_DIR / "golden_set.json"
REPORT_PATH = EVALS_DIR / "last_run_report.json"

sys.path.insert(0, str(PROJECT_ROOT))

from config import AppConfig  # noqa: E402
from evals.offline_embedding import OfflineEmbeddings  # noqa: E402
from rag_pipeline import RAGPipeline  # noqa: E402


class _OfflineLLM:
    """离线模式占位 LLM：只提供 embeddings，不真正调用大模型。"""

    chat_model = "offline"
    embedding_model = "offline"

    def __init__(self) -> None:
        self._embeddings = OfflineEmbeddings()

    def embeddings(self) -> OfflineEmbeddings:
        return self._embeddings

    def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        raise RuntimeError("离线模式不调用大模型，请去掉 --offline 运行在线评测。")

    def chat_raw(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        raise RuntimeError("离线模式不调用大模型，请去掉 --offline 运行在线评测。")

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ):
        raise RuntimeError("离线模式不调用大模型，请去掉 --offline 运行在线评测。")

    def stream_chat_raw(
        self,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ):
        raise RuntimeError("离线模式不调用大模型，请去掉 --offline 运行在线评测。")

    def web_search(self, query: str) -> str:
        raise RuntimeError("离线模式不调用大模型，请去掉 --offline 运行在线评测。")


def _offline_config(data_dir: Path) -> AppConfig:
    return AppConfig(
        data_dir=data_dir,
        chroma_dir=data_dir / "chroma",
        db_path=data_dir / "kb.sqlite3",
        chunk_size=200,
        chunk_overlap=20,
        top_k=3,
        fusion_pool=6,
        relevance_threshold=0.05,  # 伪向量相似度天然偏低
        mmr_enabled=False,
        mmr_lambda=0.7,
        query_rewrite_enabled=False,
        history_max_tokens=2000,
        rag_context_max_tokens=2500,
        agent_max_steps=3,
        embedding_batch_size=16,
    )


def build_pipeline(offline: bool) -> RAGPipeline:
    data_dir = Path(tempfile.mkdtemp(prefix="rag_eval_")) / "data"
    if offline:
        return RAGPipeline(config=_offline_config(data_dir), llm_client=_OfflineLLM())
    return RAGPipeline(config=AppConfig.from_env(data_dir=data_dir))


def load_golden_set() -> list[dict[str, Any]]:
    return json.loads(GOLDEN_SET.read_text(encoding="utf-8"))


def run_item(
    pipeline: RAGPipeline,
    kb_id: str,
    item: dict[str, Any],
    offline: bool,
) -> dict[str, Any]:
    top_k = int(item.get("top_k", 3))
    chunks = pipeline.retrieve(kb_id, item["question"], k=top_k)
    retrieved = [chunk.metadata.get("source", "") for chunk in chunks]
    result: dict[str, Any] = {
        "question": item["question"],
        "expected_source": item["expected_source"],
        "top_k": top_k,
        "hit@k": item["expected_source"] in retrieved,
        "retrieved_sources": retrieved,
    }
    if not offline:
        answer = pipeline.answer(item["question"], kb_id=kb_id)
        keywords = item.get("keywords", [])
        result["keyword_hit"] = all(k in answer["answer"] for k in keywords)
        result["citation"] = bool(answer["sources"])
        result["answer_preview"] = answer["answer"][:200]
    return result


def summarize(items: list[dict[str, Any]], offline: bool) -> dict[str, float]:
    hit = sum(1 for item in items if item["hit@k"]) / len(items)
    summary: dict[str, float] = {"hit@k": round(hit, 4)}
    if not offline:
        summary["keyword_hit"] = round(
            sum(1 for item in items if item.get("keyword_hit")) / len(items), 4
        )
        summary["citation"] = round(
            sum(1 for item in items if item.get("citation")) / len(items), 4
        )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="RAG golden set 评测")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="使用确定性伪向量，不调用真实 Embedding / LLM（仅验证链路）",
    )
    args = parser.parse_args()

    pipeline = build_pipeline(args.offline)
    kb = pipeline.create_kb("eval", "评测知识库")
    for path in sorted(SAMPLE_DOCS.glob("*.txt")):
        pipeline.upload_document(kb.id, path.name, path.read_bytes())
    pipeline.ingest(kb.id)

    items = [
        run_item(pipeline, kb.id, item, args.offline)
        for item in load_golden_set()
    ]
    summary = summarize(items, args.offline)
    report = {
        "mode": "offline" if args.offline else "online",
        "golden_set_size": len(items),
        "metrics": summary,
        "items": items,
    }
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"报告已写入：{REPORT_PATH}")


if __name__ == "__main__":
    main()
