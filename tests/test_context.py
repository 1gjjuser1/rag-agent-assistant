"""上下文预算测试：历史截断与检索片段裁剪。"""

from __future__ import annotations

from rag_pipeline import fit_context_indices, truncate_history
from utils.logger import estimate_tokens


def test_truncate_history_keeps_recent_within_budget() -> None:
    history = [
        {"role": "user", "content": "A" * 800},
        {"role": "assistant", "content": "B" * 800},
        {"role": "user", "content": "C" * 800},
    ]
    truncated = truncate_history(history, max_tokens=300)
    assert len(truncated) == 1
    assert truncated[0]["content"] == "C" * 800
    assert sum(estimate_tokens(item["content"]) for item in truncated) <= 500


def test_truncate_history_empty() -> None:
    assert truncate_history([], 100) == []
    assert truncate_history(None, 100) == []


def test_fit_context_indices_keeps_whole_blocks() -> None:
    blocks = ["x" * 400, "y" * 400, "z" * 400]  # 每块约 100 tokens
    assert fit_context_indices(blocks, max_tokens=250) == [0, 1]
    assert fit_context_indices(blocks, max_tokens=10**6) == [0, 1, 2]


def test_fit_context_indices_always_keeps_first() -> None:
    assert fit_context_indices(["x" * 4000], max_tokens=1) == [0]
