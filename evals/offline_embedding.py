"""离线确定性伪向量：仅供无 API Key 时验证评测链路，不具备语义能力。

实现为“关键词 + 中文 bigram 词袋”的稀疏向量：共享词越多，余弦相似度越高，
因此同一文档内的检索能工作，但跨文档语义相关性不做承诺。
"""

from __future__ import annotations

import re

import numpy as np

EMBEDDING_DIM = 256


class OfflineEmbeddings:
    _token_index: dict[str, int] = {}

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self.dim = dim

    @staticmethod
    def _tokens(text: str) -> set[str]:
        tokens: set[str] = set()
        for word in re.findall(r"[A-Za-z0-9_]+", (text or "").lower()):
            tokens.add(word)
        cjk = "".join(re.findall(r"[\u4e00-\u9fff]+", text or ""))
        if len(cjk) <= 2:
            if cjk:
                tokens.add(cjk)
        else:
            tokens.update(cjk[index : index + 2] for index in range(len(cjk) - 1))
        return tokens

    def _vector(self, text: str) -> list[float]:
        vec = np.zeros(self.dim)
        for token in self._tokens(text):
            index = OfflineEmbeddings._token_index.setdefault(
                token, len(OfflineEmbeddings._token_index)
            )
            if index < self.dim:
                vec[index] += 1.0
        norm = np.linalg.norm(vec) or 1.0
        return (vec / norm).tolist()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)
