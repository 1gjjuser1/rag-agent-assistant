"""通用知识库 RAG + Agent 单页应用（阶段 A）。

功能：
- 多知识库管理（创建 / 切换 / 删除）；
- 文档上传（TXT / Word / PDF / Markdown）与后台异步入库；
- 文档列表、分类/标签、版本号与删除；
- 流式聊天：Agent 自动选择工具（知识库检索 / 天气 / 联网搜索）；
- 引用来源与 Agent 执行轨迹展示。
"""

from __future__ import annotations

import contextlib
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

import streamlit as st

from config import AppConfig
from rag_pipeline import RAGPipeline
from react_agent import ReActAgent

PROJECT_ROOT = Path(__file__).resolve().parent
LOGO_PATH = Path(
    os.getenv("LOGO_PATH", str(PROJECT_ROOT / "assets" / "logo.png"))
).expanduser()

st.set_page_config(
    page_title="智能文档助手",
    page_icon="🧠",
    layout="wide",
)

st.markdown(
    """
    <style>
      [data-testid="stChatInput"] { max-width: 880px; margin: 0 auto; }
      [data-testid="stChatMessage"], .stChatMessage {
          max-width: 880px; margin-left: auto; margin-right: auto;
      }
      [data-testid="stChatMessageContent"], .stChatMessageContent { line-height: 1.75; }
      .block-container { max-width: 1180px; }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource
def get_services() -> tuple[RAGPipeline, ReActAgent]:
    rag = RAGPipeline()
    return rag, ReActAgent(rag=rag, llm_client=rag.llm)


@dataclass
class IngestJob:
    """后台入库任务状态；由工作线程写入，主线程只读。"""

    kb_id: str
    running: bool = False
    finished: bool = False
    error: str | None = None
    message: str = ""
    stage: str = "等待中"
    done: int = 0
    total: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def progress(self) -> float:
        return self.done / self.total if self.total else 0.0


def start_ingest_job(rag: RAGPipeline, kb_id: str) -> None:
    """在后台线程中入库；工作线程使用独立的 RAGPipeline 实例，完成后刷新主实例。"""
    job = IngestJob(kb_id=kb_id, running=True)
    st.session_state["ingest_job"] = job

    def _progress(stage: str, done: int, total: int) -> None:
        with job.lock:
            job.stage = stage
            job.done = done
            job.total = total

    def _run() -> None:
        try:
            worker = RAGPipeline(config=AppConfig.from_env())
            result = worker.ingest(kb_id, on_progress=_progress)
            with job.lock:
                job.message = result.get("message", "")
                job.error = result.get("error")
        except Exception as exc:
            with job.lock:
                job.error = str(exc)
        finally:
            with job.lock:
                job.running = False
                job.finished = True
            with contextlib.suppress(Exception):
                rag.refresh()

    threading.Thread(target=_run, daemon=True).start()


@st.fragment(run_every=2.0)
def render_job_status(job: IngestJob) -> None:
    """自动刷新的入库状态面板。"""
    with job.lock:
        running = job.running
        stage = job.stage
        progress = job.progress
        message = job.message
        error = job.error
    if running:
        st.info(f"后台索引中：{stage}")
        st.progress(progress)
    else:
        if error:
            st.error(f"入库失败：{error}")
        else:
            st.success(message or "入库完成。")
        st.caption("索引已刷新，现在可以提问。")


rag, agent = get_services()

# ---------- 会话状态 ----------
if "messages" not in st.session_state:
    st.session_state.messages = []
if "kb_id" not in st.session_state:
    kbs = rag.list_kbs()
    st.session_state.kb_id = kbs[0].id if kbs else None

kb_id = st.session_state.get("kb_id")

# ---------- 侧边栏：知识库管理 ----------
with st.sidebar:
    if LOGO_PATH.exists():
        st.image(str(LOGO_PATH), use_container_width=True)
    st.header("知识库管理")
    kbs = rag.list_kbs()
    kb_options = {kb.name: kb.id for kb in kbs}
    if kb_options:
        current_name = next((name for name, kid in kb_options.items() if kid == kb_id), list(kb_options)[0])
        selected_name = st.selectbox("当前知识库", list(kb_options), index=list(kb_options).index(current_name))
        kb_id = kb_options[selected_name]
        st.session_state.kb_id = kb_id
    else:
        st.info("还没有知识库，先创建一个。")

    with st.expander("新建知识库"):
        new_name = st.text_input("名称", key="new_kb_name")
        new_desc = st.text_input("描述", key="new_kb_desc")
        if st.button("创建", key="create_kb_btn"):
            if not new_name.strip():
                st.warning("请输入知识库名称。")
            else:
                try:
                    kb = rag.create_kb(new_name, new_desc)
                    st.session_state.kb_id = kb.id
                    st.success(f"已创建：{kb.name}")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

    if kb_id:
        stats = rag.kb_stats(kb_id)
        st.caption(f"{stats['documents']} 个文档 · {stats['chunks']} 个片段")
        st.divider()

        st.subheader("上传文档")
        uploads = st.file_uploader(
            "支持 TXT / Word / PDF / Markdown",
            type=["txt", "docx", "pdf", "md"],
            accept_multiple_files=True,
        )
        category = st.text_input("分类（可选）", key="upload_category")
        tags = st.text_input("标签（逗号分隔，可选）", key="upload_tags")
        if st.button("上传并后台索引", type="primary", use_container_width=True):
            saved = 0
            for uploaded in uploads or []:
                try:
                    rag.upload_document(
                        kb_id,
                        uploaded.name,
                        uploaded.getbuffer().tobytes(),
                        category=category,
                        tags=tags,
                    )
                    saved += 1
                except Exception as exc:
                    st.warning(f"{uploaded.name}：{exc}")
            if saved:
                start_ingest_job(rag, kb_id)
                st.success(f"已保存 {saved} 个文件，后台开始索引。")
                st.rerun()
            else:
                st.warning("没有可保存的文件（请先选择文件）。")

        job = st.session_state.get("ingest_job")
        if job and (job.running or job.finished):
            render_job_status(job)

        if st.button("重新索引全部文档", use_container_width=True):
            start_ingest_job(rag, kb_id)
            st.rerun()

        st.divider()
        st.subheader("已索引文档")
        docs = rag.list_documents(kb_id)
        if not docs:
            st.info("当前知识库没有文档。")
        for doc in docs:
            col1, col2, col3 = st.columns([4, 2, 1])
            col1.write(f"• {doc.filename}")
            col2.caption(f"v{doc.latest_version} · {doc.chunk_count} 片段")
            if col3.button("删除", key=f"del_{doc.id}"):
                rag.delete_document(doc.id)
                st.rerun()

        st.divider()
        confirm_delete_kb = st.checkbox(
            "确认删除该知识库（不可恢复）", key="confirm_kb_delete"
        )
        if st.button("删除当前知识库", use_container_width=True) and confirm_delete_kb:
            rag.delete_kb(kb_id)
            st.session_state.kb_id = None
            st.rerun()

    st.divider()
    st.caption(f"Agent 工具：{', '.join(agent._build_registry(kb_id).names())}")
    st.caption("天气数据来自 Open-Meteo，无需 API Key。")
    if st.button("🗑 清空对话", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

# ---------- 主区域：聊天 ----------
st.title("🧠 智能文档助手")
if kb_id and (current_kb := rag.get_kb(kb_id)):
    kb_stats = rag.kb_stats(kb_id)
    st.caption(
        f"当前知识库：**{current_kb.name}**（{kb_stats['documents']} 个文档 · "
        f"{kb_stats['chunks']} 个片段）· 知识库 RAG + Agent 工具调度"
    )
else:
    st.caption("未选择知识库：普通聊天模式 · 可在左侧创建或切换知识库")


def history_messages() -> list[dict[str, str]]:
    return [
        {"role": msg["role"], "content": msg["content"]}
        for msg in st.session_state.messages[:-1]
    ]


for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        if message.get("usage"):
            usage = message["usage"]
            st.caption(
                "上下文约 "
                f"{usage.get('context_tokens', 0)} tokens（预估）· "
                f"实际输入 {usage.get('prompt_tokens', '?')} / "
                f"输出 {usage.get('completion_tokens', '?')} tokens"
            )
        if message.get("sources"):
            with st.expander(f"查看引用来源（{len(message['sources'])}）"):
                for index, source in enumerate(message["sources"], start=1):
                    st.markdown(f"**{index}. {source['citation']}**")
                    st.caption(source.get("content", ""))
                    st.caption(
                        "向量相似度 "
                        f"{source.get('vector_score', '?')} · BM25 {source.get('bm25_score', '?')}"
                    )
        if message.get("steps"):
            with st.expander("查看 Agent 执行轨迹"):
                for step in message["steps"]:
                    st.json(step)

# 示例问题快捷入口（点击即提问）
col_a, col_b, col_c = st.columns(3)
pending_question = None
if col_a.button("📄 公司的主营业务是什么", use_container_width=True):
    pending_question = "公司的主营业务是什么？"
if col_b.button("🌤 郑州今天是什么天气？", use_container_width=True):
    pending_question = "郑州今天是什么天气？"

question = st.chat_input(
    "输入问题后回车发送：可提问文档、查询天气、或直接聊天",
    max_chars=1000,
)
if question is None:
    question = pending_question

if question:
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        answer_box = st.empty()
        status_box = st.empty()
        answer_text = ""
        steps: list[dict] = []
        sources: list[dict] = []
        context_tokens = 0
        for event in agent.run_stream(
            question,
            history=history_messages(),
            kb_id=st.session_state.get("kb_id"),
        ):
            event_type = event["type"]
            if event_type == "tool_start":
                status_box.caption(f"🔧 正在调用 {event['tool']}：{event['args']}")
            elif event_type == "tool_end":
                status_box.caption(f"✔ {event['tool']} 完成")
                sources.extend(event.get("sources") or [])
                context_tokens = max(
                    context_tokens,
                    (event.get("extra") or {}).get("context_tokens", 0),
                )
            elif event_type == "step":
                steps.append(event["step"])
            elif event_type == "token":
                answer_text += event["text"]
                answer_box.markdown(answer_text)
            elif event_type == "done":
                answer_text = event["answer"]
                answer_box.markdown(answer_text)
            elif event_type == "error":
                status_box.error(event["message"])

    actual_usage = getattr(rag.llm, "last_usage", None) or {}
    st.session_state.messages.append(
        {
            "role": "assistant",
            "content": answer_text,
            "sources": sources,
            "steps": steps,
            "usage": {
                "context_tokens": context_tokens,
                "prompt_tokens": actual_usage.get("prompt_tokens"),
                "completion_tokens": actual_usage.get("completion_tokens"),
            },
        }
    )
    # 最多保留最近 10 轮（20 条消息）。
    st.session_state.messages = st.session_state.messages[-20:]
