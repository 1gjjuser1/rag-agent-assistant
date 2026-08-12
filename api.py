"""FastAPI 服务层：把 RAG / Agent 能力暴露为 REST API（阶段 B 的第一步）。

特性：
- 路由前缀 ``/v1``，JSON 输入输出，支持知识库管理、文档上传、入库与问答；
- 可选 Bearer Token 鉴权：设置 ``API_AUTH_TOKEN`` 后所有 ``/v1`` 接口需要
  ``Authorization: Bearer <token>``；未设置时开放访问（仅建议本机/内网调试）；
- 文档上传使用 multipart 表单（与 Streamlit 页面共用同一套 RAGPipeline 逻辑）。

启动：
    uvicorn api:app --host 0.0.0.0 --port 8000

注意：当前为单机演示服务，用全局锁串行化读写，避免 SQLite / Chroma
并发访问问题；多租户与任务队列是下一阶段的演进方向。
"""

from __future__ import annotations

import os
import threading
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from llm_client import safe_error
from rag_pipeline import RAGPipeline
from react_agent import ReActAgent

API_AUTH_TOKEN = os.getenv("API_AUTH_TOKEN", "").strip()
_service_lock = threading.RLock()

app = FastAPI(
    title="智能文档助手 API",
    description="企业知识库 RAG + Function Calling Agent 的 REST 服务层",
    version="1.0.0",
)
security = HTTPBearer(auto_error=False)


def require_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> None:
    """未配置 API_AUTH_TOKEN 时放行；配置后校验 Bearer Token。"""
    if not API_AUTH_TOKEN:
        return
    if credentials is None or credentials.credentials != API_AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="无效或缺失的 API Token")


def get_services() -> tuple[RAGPipeline, ReActAgent]:
    rag = RAGPipeline()
    return rag, ReActAgent(rag=rag, llm_client=rag.llm)


rag, agent = get_services()


# ---------- 数据模型 ----------


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000, description="用户问题")
    history: list[dict[str, str]] = Field(
        default_factory=list,
        description="最近对话（user/assistant 交替）",
    )
    kb_id: str | None = Field(default=None, description="知识库 id；不传则用默认库")


class ChatResponse(BaseModel):
    answer: str
    sources: list[dict[str, Any]] = Field(default_factory=list)
    steps: list[dict[str, Any]] = Field(default_factory=list)
    kb_id: str | None = None


class KbCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = ""


class UploadResponse(BaseModel):
    doc_id: str
    filename: str
    version: int


def _ensure_kb(kb_id: str) -> None:
    if rag.get_kb(kb_id) is None:
        raise HTTPException(status_code=404, detail=f"知识库不存在：{kb_id}")


# ---------- 路由 ----------


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/kbs", dependencies=[Depends(require_auth)])
def list_kbs() -> list[dict[str, Any]]:
    with _service_lock:
        return [kb.__dict__ for kb in rag.list_kbs()]


@app.post("/v1/kbs", dependencies=[Depends(require_auth)])
def create_kb(body: KbCreateRequest) -> dict[str, Any]:
    try:
        with _service_lock:
            return rag.create_kb(body.name, body.description).__dict__
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.delete("/v1/kbs/{kb_id}", dependencies=[Depends(require_auth)])
def delete_kb(kb_id: str) -> dict[str, str]:
    with _service_lock:
        _ensure_kb(kb_id)
        rag.delete_kb(kb_id)
    return {"deleted": kb_id}


@app.post("/v1/kbs/{kb_id}/documents", dependencies=[Depends(require_auth)])
async def upload_document(
    kb_id: str,
    file: UploadFile = File(..., description="文档文件（txt/docx/pdf/md）"),
    category: str = Form(""),
    tags: str = Form(""),
) -> UploadResponse:
    with _service_lock:
        _ensure_kb(kb_id)
        data = await file.read()
        try:
            doc = rag.upload_document(
                kb_id,
                file.filename or "upload",
                data,
                category=category,
                tags=tags,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return UploadResponse(
        doc_id=doc.id,
        filename=doc.filename,
        version=doc.latest_version,
    )


@app.post("/v1/kbs/{kb_id}/ingest", dependencies=[Depends(require_auth)])
def ingest_kb(kb_id: str) -> dict[str, Any]:
    with _service_lock:
        _ensure_kb(kb_id)
        result = rag.ingest(kb_id)
    if result.get("error"):
        raise HTTPException(status_code=500, detail=result["error"])
    return result


@app.get("/v1/kbs/{kb_id}/documents", dependencies=[Depends(require_auth)])
def list_documents(kb_id: str) -> list[dict[str, Any]]:
    with _service_lock:
        _ensure_kb(kb_id)
        return rag.list_indexed_files(kb_id)


@app.get("/v1/kbs/{kb_id}/stats", dependencies=[Depends(require_auth)])
def kb_stats(kb_id: str) -> dict[str, Any]:
    with _service_lock:
        _ensure_kb(kb_id)
        return rag.kb_stats(kb_id)


@app.delete("/v1/documents/{doc_id}", dependencies=[Depends(require_auth)])
def delete_document(doc_id: str) -> dict[str, str]:
    with _service_lock:
        if not rag.delete_document(doc_id):
            raise HTTPException(status_code=404, detail=f"文档不存在：{doc_id}")
    return {"deleted": doc_id}


@app.post("/v1/chat", dependencies=[Depends(require_auth)])
def chat(body: ChatRequest) -> ChatResponse:
    if not body.question.strip():
        raise HTTPException(status_code=400, detail="question 不能为空")
    try:
        with _service_lock:
            result = agent.run(body.question, history=body.history, kb_id=body.kb_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=safe_error(exc)) from exc
    return ChatResponse(
        answer=result.answer,
        sources=result.sources,
        steps=result.steps,
        kb_id=body.kb_id,
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
