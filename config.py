"""集中配置：所有可调参数从环境变量读取，带默认值。

阶段 A 的目标之一是让 RAG 的切分、检索、重排等行为可配置且可复现，
因此把散落在各模块里的魔法数字集中到这里。所有配置项同时支持环境变量
覆盖（见 `.env.example`），方便部署时调整而不改代码。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name, default)
    return value.strip() if value else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _clamp(value: int, low: int, high: int) -> int:
    return min(max(value, low), high)


def _resolve_path(value: str, base: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base / path


@dataclass(frozen=True)
class AppConfig:
    """RAG/Agent 阶段 A 的全部运行参数（使用了 Python 的 @dataclass(frozen=True) 装饰器，冻结对象，避免运行中被意外修改）。

    字段说明：
    - data_dir / chroma_dir / db_path：数据、向量、元数据存储位置；
    - chunk_size / chunk_overlap：文本切分参数；
    - top_k：最终返回给大模型的片段数；
    - fusion_pool：混合检索时向量与 BM25 各自取回并参与融合的候选数；
    - relevance_threshold：向量余弦相似度门槛，低于该值的查询视为“无相关内容”；
    - mmr_enabled / mmr_lambda：是否启用 MMR 去重重排及多样性权重；
    - query_rewrite_enabled：多轮对话时是否先用大模型改写问题；
    - agent_max_steps：Agent 最多调用工具的轮数；
    - embedding_batch_size：向量化时的批量大小。
    """

    data_dir: Path
    chroma_dir: Path
    db_path: Path

    chunk_size: int
    chunk_overlap: int
    top_k: int
    fusion_pool: int
    relevance_threshold: float
    mmr_enabled: bool
    mmr_lambda: float
    query_rewrite_enabled: bool
    history_max_tokens: int
    rag_context_max_tokens: int
    agent_max_steps: int
    embedding_batch_size: int

    @classmethod
    def from_env(cls, data_dir: str | Path | None = None) -> AppConfig:
        """从环境变量构建配置；data_dir 可显式指定（测试用临时目录）。"""
        base = Path(data_dir).resolve() if data_dir else PROJECT_ROOT / "data"
        # DashScope Embedding 接口单次最多 20 条（qwen3-text-embedding 等），
        # 默认取 16 留出余量，避免“batch size should not be larger than 20”报错。
        embedding_batch_size = _clamp(_env_int("EMBEDDING_BATCH_SIZE", 16), 1, 20)
        return cls(
            data_dir=base,
            chroma_dir=_resolve_path(_env_str("CHROMA_DIR", str(base / "chroma")), base),
            db_path=_resolve_path(_env_str("KB_DB_PATH", str(base / "kb.sqlite3")), base),
            chunk_size=_env_int("CHUNK_SIZE", 500),
            chunk_overlap=_env_int("CHUNK_OVERLAP", 50),
            top_k=_env_int("RETRIEVAL_TOP_K", 5),
            fusion_pool=_env_int("RETRIEVAL_FUSION_POOL", 12),
            relevance_threshold=_env_float("RETRIEVAL_RELEVANCE_THRESHOLD", 0.3),
            mmr_enabled=_env_bool("RETRIEVAL_MMR_ENABLED", True),
            mmr_lambda=_env_float("RETRIEVAL_MMR_LAMBDA", 0.7),
            query_rewrite_enabled=_env_bool("QUERY_REWRITE_ENABLED", True),
            history_max_tokens=_env_int("HISTORY_MAX_TOKENS", 2000),
            rag_context_max_tokens=_env_int("RAG_CONTEXT_MAX_TOKENS", 2500),
            agent_max_steps=_env_int("AGENT_MAX_STEPS", 5),
            embedding_batch_size=embedding_batch_size,
        )
