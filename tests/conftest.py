"""pytest 夹具：把 helpers 中的假组件组装成 pipeline / agent。"""

from __future__ import annotations

import pytest

from rag_pipeline import RAGPipeline
from react_agent import ReActAgent
from tests.helpers import FakeLLMClient, make_config


@pytest.fixture
def pipeline(tmp_path) -> RAGPipeline:
    return RAGPipeline(
        config=make_config(tmp_path),
        llm_client=FakeLLMClient(),
    )


@pytest.fixture
def agent(pipeline: RAGPipeline) -> ReActAgent:
    return ReActAgent(rag=pipeline, llm_client=pipeline.llm, max_steps=3)
