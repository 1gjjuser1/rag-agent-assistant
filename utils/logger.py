"""线程安全的 Agent JSONL 轨迹日志。"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AgentTraceLogger:
    """逐行追加 JSON，单条写入失败不会影响 Agent 主流程。"""

    _lock = threading.Lock()

    def __init__(self, path: str | Path = "data/agent_trace.jsonl") -> None:
        self.path = Path(path)

    def log(
        self,
        *,
        step: int,
        thought_summary: str,
        tool: str | None,
        args: dict[str, Any],
        observation: str,
        cost_estimate: int,
    ) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "step": int(step),
            "thought_summary": str(thought_summary),
            "tool": tool,
            "args": args,
            "observation": str(observation),
            "cost_estimate": int(cost_estimate),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock, self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            # 日志系统不得拖垮业务流程。
            pass


def estimate_tokens(*texts: str) -> int:
    """中英文混合文本的轻量 token 估算。

    中文（含中文标点）约 1 token/字，英文/数字约 4 字符/token。
    用于历史与上下文预算截断，不需要非常精确，但不能系统性低估中文。
    """
    total = 0.0
    for text in texts:
        text = text or ""
        for char in text:
            if "\u4e00" <= char <= "\u9fff" or "\u3000" <= char <= "\u303f":
                total += 1.0
            else:
                total += 0.25
    return max(1, int(total))


if __name__ == "__main__":
    logger = AgentTraceLogger("data/agent_trace.demo.jsonl")
    logger.log(
        step=1,
        thought_summary="日志模块自检",
        tool=None,
        args={},
        observation="ok",
        cost_estimate=1,
    )
    print("日志自检完成：data/agent_trace.demo.jsonl")
