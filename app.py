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
      /* 隐藏 Streamlit 默认装饰。
         注意：不能隐藏 header / stToolbar —— 侧边栏折叠后的“展开”按钮
         stExpandSidebarButton 就挂在 stToolbar 里，隐藏会导致侧边栏无法恢复。 */
      #MainMenu, footer, [data-testid="stDecoration"] { display: none; }
      [data-testid="stToolbar"] [data-testid="stToolbarActions"] { display: none; }
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
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      /* 会话行：平时隐藏 ⋯ 菜单按钮，鼠标悬停整行时显示 */
      div[class*="st-key-conv_row_"] button[data-testid="stPopoverButton"] {
        opacity: 0;
        pointer-events: none;
        border: none;
        background: transparent;
        box-shadow: none;
        color:rgb(114, 70, 70);
        min-width: 1.8rem;
        padding: 0 0.2rem;
        transition: opacity 0.15s ease;
      }
      div[class*="st-key-conv_row_"]:hover button[data-testid="stPopoverButton"] {
        opacity: 1;
        pointer-events: auto;
      }

      /* 欢迎页：居中标题 + 居中输入框（仿 DeepSeek 首页） */
      .welcome { text-align: center; padding-top: 22vh; }
      .welcome h1 { font-size: 1.9rem; font-weight: 600; color: #1f1f1f; }

      /* 欢迎页输入表单：去掉默认边框，圆角居中 */
      form[data-testid="stForm"] {
        border: none;
        background: transparent;
        padding: 0;
        max-width: 880px;
        margin: 0 auto;
      }
      form[data-testid="stForm"] [data-testid="stTextInput"] input {
        border-radius: 26px;
        border: 1px solid #e5e5e5;
        box-shadow: 0 2px 10px rgba(0, 0, 0, 0.05);
        padding: 0.85rem 1.2rem;
      }
      form[data-testid="stForm"] [data-testid="stTextInput"] input:focus {
        border-color: #4d6bfe;
        box-shadow: 0 2px 14px rgba(77, 107, 254, 0.15);
      }

      /* 当前会话高亮 */
      .conv-title { font-weight: 600; color: #1f1f1f; }
    </style>
    """,
    unsafe_allow_html=True,
)


#做了什么： 创建两个核心服务对象，并用 @st.cache_resource 缓存，保证整个 Streamlit 应用只创建一次,而不是每次刷新页面都重新创建

@st.cache_resource
def get_services() -> tuple[RAGPipeline, ReActAgent]:
    rag = RAGPipeline()
    return rag, ReActAgent(rag=rag, llm_client=rag.llm)


#定义了一个"入库任务"类。当用户上传文档后，系统需要在后台把文档切块、生成向量索引，这个过程比较耗时，所以放到后台线程去做。
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
def _new_conversation(kb_id, title="新对话") -> dict:
    return {
        "id": uuid.uuid4().hex[:10],   # 随机唯一ID
        "title": title,                 # 会话标题
        "messages": [],                 # 消息历史
        "kb_id": kb_id,                 # 绑定的知识库
        "pinned": False,               # 是否置顶
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

    # 对话面板：新建 / 切换 / 管理（重命名 / 置顶 / 删除）
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
        history_items = [
            (cid, conv) for cid, conv in convs.items() if conv.get("messages")
        ]
        if not history_items:
            st.caption("暂无历史对话")
        else:
            newest_first = list(reversed(history_items))
            display_items = [
                kv for kv in newest_first if kv[1].get("pinned")
            ] + [kv for kv in newest_first if not kv[1].get("pinned")]
            for cid, conv in display_items:
                label = conv.get("title") or "新对话"
                if conv.get("pinned"):
                    label = f"📌 {label}"
                if cid == current_id:
                    label = f"▸ {label}"
                with st.container(key=f"conv_row_{cid}"):
                    col1, col2 = st.columns([5, 1])
                    with col1:
                        if st.button(label, key=f"open_{cid}", width="stretch"):
                            st.session_state.current_conv_id = cid
                            st.rerun()
                    with col2, st.popover("⋯", key=f"menu_{cid}"):
                        new_title = st.text_input(
                            "会话名称",
                            value=conv.get("title", ""),
                            key=f"rename_input_{cid}",
                        )
                        if st.button(
                            "保存名称", key=f"rename_save_{cid}", width="stretch"
                        ):
                            title = new_title.strip()
                            if title:
                                conv["title"] = title
                            st.rerun()
                        st.divider()
                        if st.button(
                            "取消置顶" if conv.get("pinned") else "置顶会话",
                            key=f"pin_{cid}",
                            width="stretch",
                        ):
                            conv["pinned"] = not conv.get("pinned")
                            st.rerun()
                        if st.button("删除会话", key=f"del_{cid}", width="stretch"):
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

# 聊天状态显示当前知识库信息；欢迎页保持简洁
if messages:
    if kb_id and (selected_kb := rag.get_kb(kb_id)):
        kb_stats = rag.kb_stats(kb_id)
        st.caption(
            f"当前知识库：**{selected_kb.name}**（{kb_stats['documents']} 个文档 · "
            f"{kb_stats['chunks']} 个片段）· 知识库 RAG + Agent 工具调度"
        )
    else:
        st.caption("未选择知识库：普通聊天模式 · 可在左侧创建或切换知识库")


# 欢迎页（空会话）：居中标题 + 居中输入框（仿 DeepSeek 首页）
if not messages:
    st.markdown(
        '<div class="welcome"><h1>欢迎使用辉煌科技知识库问答系统</h1></div>',
        unsafe_allow_html=True,
    )
    with st.form("welcome_form", clear_on_submit=True):
        col_in, col_btn = st.columns([5, 1])
        with col_in:
            welcome_text = st.text_input(
                "提问",
                placeholder="给智能文档助手发送消息…",
                label_visibility="collapsed",
            )
        with col_btn:
            submitted = st.form_submit_button("发送", width="stretch")
    if submitted and welcome_text.strip():
        # 先写入用户消息并跳转：下一次渲染直接进入聊天界面（消息流 + 底部输入框），
        # 不再停留在欢迎页上输出答案。
        current["messages"].append(
            {"role": "user", "content": welcome_text.strip()}
        )
        if len(current["messages"]) == 1:
            current["title"] = _conversation_title_from(welcome_text.strip())
        st.session_state.pending_question = welcome_text.strip()
        st.rerun()


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


question = st.chat_input("给智能文档助手发送消息…", max_chars=1000) if messages else None
if question is None:
    question = st.session_state.pop("pending_question", None)

if question:
    # 欢迎页提交时已把用户消息写入会话（并已由上方消息循环渲染），
    # 这里避免重复追加/重复渲染；普通聊天输入则正常追加。
    last = current["messages"][-1] if current["messages"] else None
    already_queued = bool(
        last and last["role"] == "user" and last["content"] == question
    )
    if not already_queued:
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
