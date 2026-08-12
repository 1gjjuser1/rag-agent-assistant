"""智能文档助手 —— DeepSeek 风格简洁界面，支持多会话独立。

界面参考 DeepSeek 网页版：
- 左侧「对话」面板：开启新对话 / 历史会话列表 / 删除会话，
  每个会话独立保存消息记录与所选知识库，互不影响；
- 左侧「知识库」面板：多知识库管理、文档上传与后台异步入库；
- 主区域：干净聊天流，空会话显示欢迎页与示例问题，底部圆角输入框。
"""

from __future__ import annotations

import contextlib
import os
import threading
import uuid
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
    initial_sidebar_state="expanded",
)

# ---------- DeepSeek 风格全局样式 ----------
st.markdown(
    """
    <style>
      /* 隐藏 Streamlit 默认装饰 */
      #MainMenu, footer, header,
      [data-testid="stDecoration"], [data-testid="stToolbar"] {
        display: none;
      }
      .block-container {
        padding-top: 1.2rem;
        padding-bottom: 1.5rem;
        max-width: 1100px;
      }

      /* 全局字体：贴近 DeepSeek 的中文无衬线排版 */
      html, body, .stApp, [class*="css"] {
        font-family: -apple-system, BlinkMacSystemFont, "PingFang SC",
                     "Microsoft YaHei", "Segoe UI", "Helvetica Neue", sans-serif;
      }

      /* 聊天消息 */
      [data-testid="stChatMessage"] {
        max-width: 880px;
        margin-left: auto;
        margin-right: auto;
        padding: 0.35rem 0.15rem;
      }
      [data-testid="stChatMessageContent"] {
        font-size: 15px;
        line-height: 1.85;
        color: #1f1f1f;
        overflow-wrap: anywhere;
      }
      [data-testid="stChatMessageContent"] p {
        margin-bottom: 0.55rem;
      }
      [data-testid="stChatMessageContent"] p:last-child {
        margin-bottom: 0;
      }

      /* 输入框：圆角、居中、聚焦高亮 */
      [data-testid="stChatInput"] {
        max-width: 880px;
        margin: 0.5rem auto 0;
        border-radius: 26px;
        border: 1px solid #e5e5e5;
        box-shadow: 0 2px 10px rgba(0, 0, 0, 0.05);
      }
      [data-testid="stChatInput"]:focus-within {
        border-color: #4d6bfe;
        box-shadow: 0 2px 14px rgba(77, 107, 254, 0.15);
      }
      [data-testid="stChatInput"] textarea {
        border-radius: 26px;
      }

      /* 侧边栏 */
      section[data-testid="stSidebar"] {
        background: #fafafa;
      }
      section[data-testid="stSidebar"] .stButton button {
        border-radius: 10px;
        justify-content: flex-start;
      }

      /* 欢迎页 */
      .welcome { text-align: center; padding-top: 9vh; }
      .welcome h1 { font-size: 2rem; font-weight: 600; color: #1f1f1f; }
      .welcome p { color: #8a8a8a; font-size: 0.95rem; margin-top: 0.5rem; }

      /* 当前会话高亮 */
      .conv-title { font-weight: 600; color: #1f1f1f; }
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


# ---------- 会话（Conversation）模型：每个会话独立 ----------


def _new_conversation(kb_id: str | None, title: str = "新对话") -> dict:
    return {
        "id": uuid.uuid4().hex[:10],
        "title": title,
        "messages": [],
        "kb_id": kb_id,
    }


def _current_conversation() -> dict:
    return st.session_state.conversations[st.session_state.current_conv_id]


def _conversation_title_from(text: str) -> str:
    text = " ".join(text.split())
    return text[:18] + ("…" if len(text) > 18 else "")


def _init_conversations() -> None:
    if "conversations" not in st.session_state or not st.session_state.conversations:
        kbs = rag.list_kbs()
        default_kb = kbs[0].id if kbs else None
        conv = _new_conversation(kb_id=default_kb)
        st.session_state.conversations = {conv["id"]: conv}
        st.session_state.current_conv_id = conv["id"]


def _history_messages() -> list[dict[str, str]]:
    """当前会话中除最后一条（正在回答的问题）外的历史。"""
    conv = _current_conversation()
    return [
        {"role": msg["role"], "content": msg["content"]}
        for msg in conv["messages"][:-1]
    ]


_init_conversations()


# ---------- 侧边栏 ----------

with st.sidebar:
    if LOGO_PATH.exists():
        st.image(str(LOGO_PATH), width="stretch")

    tab_chat, tab_kb = st.tabs(["💬 对话", "📚 知识库"])

    # 对话面板：新建 / 切换 / 删除 / 清空
    with tab_chat:
        if st.button(
            "＋ 开启新对话",
            type="primary",
            width="stretch",
            key="new_conv_btn",
        ):
            kb_id = _current_conversation().get("kb_id")
            conv = _new_conversation(kb_id=kb_id)
            st.session_state.conversations[conv["id"]] = conv
            st.session_state.current_conv_id = conv["id"]
            st.rerun()

        st.divider()
        convs = st.session_state.conversations
        current_id = st.session_state.current_conv_id
        for cid, conv in reversed(list(convs.items())):
            label = conv.get("title") or "新对话"
            if cid == current_id:
                label = f"▸ {label}"
            col1, col2 = st.columns([5, 1])
            with col1:
                if st.button(label, key=f"open_{cid}", width="stretch"):
                    st.session_state.current_conv_id = cid
                    st.rerun()
            with col2:
                if st.button("✕", key=f"del_{cid}", help="删除该对话"):
                    del st.session_state.conversations[cid]
                    if st.session_state.current_conv_id == cid:
                        if st.session_state.conversations:
                            st.session_state.current_conv_id = next(
                                iter(st.session_state.conversations)
                            )
                        else:
                            conv = _new_conversation(kb_id=None)
                            st.session_state.conversations[conv["id"]] = conv
                            st.session_state.current_conv_id = conv["id"]
                    st.rerun()

        st.divider()
        if st.button("🗑 清空当前对话", width="stretch", key="clear_conv_btn"):
            _current_conversation()["messages"] = []
            st.rerun()

    # 知识库面板：管理与上传
    with tab_kb:
        current = _current_conversation()
        kbs = rag.list_kbs()
        kb_options = {kb.name: kb.id for kb in kbs}

        if kb_options:
            kb_id = current.get("kb_id")
            names = list(kb_options)
            if kb_id not in kb_options.values():
                kb_id = kb_options[names[0]]
                current["kb_id"] = kb_id
            current_name = next(
                (name for name, kid in kb_options.items() if kid == kb_id),
                names[0],
            )
            selected_name = st.selectbox(
                "当前知识库",
                names,
                index=names.index(current_name),
                key="kb_select",
            )
            current["kb_id"] = kb_options[selected_name]
            kb_id = current["kb_id"]
        else:
            st.info("还没有知识库，先创建一个。")
            kb_id = None

        with st.expander("新建知识库", expanded=not kbs):
            new_name = st.text_input("名称", key="new_kb_name")
            new_desc = st.text_input("描述", key="new_kb_desc")
            if st.button("创建", key="create_kb_btn"):
                if not new_name.strip():
                    st.warning("请输入知识库名称。")
                else:
                    try:
                        kb = rag.create_kb(new_name, new_desc)
                        current["kb_id"] = kb.id
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
            if st.button("上传并后台索引", type="primary", width="stretch"):
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

            if st.button("重新索引全部文档", width="stretch"):
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
            if (
                st.button("删除当前知识库", width="stretch")
                and confirm_delete_kb
            ):
                rag.delete_kb(kb_id)
                if current.get("kb_id") == kb_id:
                    current["kb_id"] = None
                st.rerun()

        st.divider()
        st.caption(f"Agent 工具：{', '.join(agent._build_registry(kb_id).names())}")
        st.caption("天气数据来自 Open-Meteo，无需 API Key。")


# ---------- 主区域：聊天 ----------

current = _current_conversation()
kb_id = current.get("kb_id")
messages = current["messages"]

if kb_id and (kb := rag.get_kb(kb_id)):
    kb_stats = rag.kb_stats(kb_id)
    st.caption(
        f"当前知识库：**{kb.name}**（{kb_stats['documents']} 个文档 · "
        f"{kb_stats['chunks']} 个片段）· 知识库 RAG + Agent 工具调度"
    )
else:
    st.caption("未选择知识库：普通聊天模式 · 可在左侧创建或切换知识库")


# 欢迎页（空会话）
pending_question = None
if not messages:
    st.markdown(
        '<div class="welcome"><h1>有什么可以帮你？</h1>'
        "<p>可以提问文档、查询天气、查数据库，或直接聊天</p></div>",
        unsafe_allow_html=True,
    )
    col_a, col_b, col_c = st.columns(3)
    if col_a.button("📄 公司的主营业务是什么", width="stretch"):
        pending_question = "公司的主营业务是什么？"
    if col_b.button("🌤 郑州今天是什么天气？", width="stretch"):
        pending_question = "郑州今天是什么天气？"
    if col_c.button("🗄 数据库里有哪些表？", width="stretch"):
        pending_question = "数据库里有哪些表？"


for msg in messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("usage"):
            usage = msg["usage"]
            st.caption(
                "上下文约 "
                f"{usage.get('context_tokens', 0)} tokens（预估）· "
                f"实际输入 {usage.get('prompt_tokens', '?')} / "
                f"输出 {usage.get('completion_tokens', '?')} tokens"
            )
        if msg.get("sources"):
            with st.expander(f"查看引用来源（{len(msg['sources'])}）"):
                for index, source in enumerate(msg["sources"], start=1):
                    st.markdown(f"**{index}. {source['citation']}**")
                    st.caption(source.get("content", ""))
                    st.caption(
                        "向量相似度 "
                        f"{source.get('vector_score', '?')} · BM25 "
                        f"{source.get('bm25_score', '?')}"
                    )
        if msg.get("steps"):
            with st.expander("查看 Agent 执行轨迹"):
                for step in msg["steps"]:
                    st.json(step)


question = st.chat_input(
    "给智能文档助手发送消息…",
    max_chars=1000,
)
if question is None:
    question = pending_question

if question:
    current["messages"].append({"role": "user", "content": question})
    if len(current["messages"]) == 1:
        current["title"] = _conversation_title_from(question)

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
            history=_history_messages(),
            kb_id=st.session_state.conversations[
                st.session_state.current_conv_id
            ].get("kb_id"),
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
    current["messages"].append(
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
