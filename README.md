# 智能文档助手 RAG + Agent（阶段 A）

[![CI](https://github.com/1gjjuser1/rag-agent-assistant/actions/workflows/ci.yml/badge.svg)](https://github.com/1gjjuser1/rag-agent-assistant/actions/workflows/ci.yml)

基于 Python 的企业内部知识库问答原型：**多知识库 RAG + Function Calling Agent + 流式对话**。
这是对原版“单知识库 MVP”的阶段 A 升级，目标是把一个演示应用改造成可靠的单机产品，
为阶段 B（多租户平台化）打好地基。

## 功能特性

- **多知识库**：独立创建 / 切换 / 删除，每个知识库有独立的向量索引与关键词索引；
- **混合检索**：向量语义检索 + BM25 词面检索，RRF 融合排序，可选 MMR 去重重排；
- **相关性门槛**：向量与 BM25 都没有像样命中才拒答，避免幻觉；BM25 强命中时不会被向量阈值误杀；
- **增量入库**：SHA-256 判重，只处理变化的文件；切分参数或 Embedding 模型变更时自动全量重建；
- **文档版本**：同名文件重传自动升版本，旧版本文件保留在磁盘；
- **真正的 Agent**：LLM 通过 Function Calling 自主选择工具（知识库 / 天气 / 联网搜索 / 只读数据库）、
  多步循环执行，而不是原来的关键词路由；
- **真流式回答**：Agent 每轮直接走流式接口，回答边生成边渲染（非生成后切块模拟），工具调用过程实时展示；
- **后台入库**：上传后异步索引，不阻塞聊天；
- **引用可追溯**：每个回答附带来源文件、段落/页码与相似度得分；
- **REST API + 鉴权**：FastAPI 服务层暴露知识库管理与问答接口，支持 Bearer Token 鉴权；
- **评测体系**：golden set 评测脚本量化检索 hit@k / 回答关键词命中率 / 可溯源率；
- **离线可测**：46 项 mock 测试（pytest）+ ruff + mypy + CI，不依赖外网也能验证。

## 快速开始

环境要求：Python 3.10+，阿里云 DashScope API Key。

```bash
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
Copy-Item .env.example .env   # Windows
# cp .env.example .env        # macOS / Linux
```

编辑 `.env` 填入 `DASHSCOPE_API_KEY` 后启动：

```bash
streamlit run app.py
```

首次启动会自动把 `data/docs/` 根目录遗留的示例文档导入“默认知识库”；
在侧边栏点“上传并后台索引”即可完成向量化（无需手工复制文件）。

### REST API（可选）

```bash
# 设置鉴权 Token 后启动（不设置则开放访问，仅建议本机调试）
uvicorn api:app --host 0.0.0.0 --port 8000

# 问答
curl -X POST http://localhost:8000/v1/chat \
  -H "Authorization: Bearer $API_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"question": "公司主营产品是什么？", "kb_id": "default"}'

# 上传文档并入库
curl -X POST -F "file=@产品介绍.txt" http://localhost:8000/v1/kbs/default/documents
curl -X POST http://localhost:8000/v1/kbs/default/ingest
```

### 评测（可选）

```bash
python evals/run_eval.py --offline   # 离线：无需 API Key，验证评测链路
python evals/run_eval.py             # 在线：真实 Embedding + LLM，得到可写进简历的指标
```

## 独立运行与测试

```bash
# 查看知识库状态（不调用 API）
python rag_pipeline.py

# 命令行 Agent（输入 exit 退出）
python react_agent.py

# 测试 DashScope 连接
python llm_client.py

# 运行全部离线测试（不消耗 API）
pytest

# 代码规范检查
ruff check .
```

## 配置说明

所有配置都可通过环境变量或 `.env` 覆盖，无需改代码：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DASHSCOPE_API_KEY` | - | 必填，阿里云百炼密钥 |
| `DASHSCOPE_BASE_URL` | 见 `.env.example` | 百炼 OpenAI 兼容接口地址 |
| `DASHSCOPE_CHAT_MODEL` | `qwen3.7-max` | 对话模型 |
| `DASHSCOPE_EMBEDDING_MODEL` | `text-embedding-v3` | Embedding 模型 |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | 500 / 50 | 文本切分参数 |
| `RETRIEVAL_TOP_K` | 5 | 最终送入大模型的片段数 |
| `RETRIEVAL_FUSION_POOL` | 12 | 向量与 BM25 各自取回的候选数 |
| `RETRIEVAL_RELEVANCE_THRESHOLD` | 0.3 | 向量余弦相似度门槛（低于则视为无相关内容） |
| `RETRIEVAL_MMR_ENABLED` | true | 是否启用 MMR 去重重排 |
| `RETRIEVAL_MMR_LAMBDA` | 0.7 | MMR 多样性权重（越大越看重相关性） |
| `QUERY_REWRITE_ENABLED` | true | 多轮对话是否先改写追问题 |
| `HISTORY_MAX_TOKENS` | 2000 | 对话历史 token 预算（防止上下文过长、注意力发散） |
| `RAG_CONTEXT_MAX_TOKENS` | 2500 | 检索片段送入大模型的 token 预算 |
| `AGENT_MAX_STEPS` | 5 | Agent 最大工具调用轮数 |
| `EMBEDDING_BATCH_SIZE` | 16 | Embedding 批量大小（DashScope 单次最多 20 条，超出会报 batch size 错误） |
| `LOGO_PATH` | `assets/logo.png` | 页面 Logo 图片路径（可选） |

## 目录结构

```text
app.py               Streamlit 界面（多知识库管理 / 后台入库 / 流式聊天）
api.py               FastAPI 服务层（REST 接口 + Bearer Token 鉴权）
config.py            集中配置（全部可调参数）
llm_client.py        DashScope 封装（对话 / 流式 / Function Calling / Embedding）
rag_pipeline.py      RAG 管线（知识库 / 入库 / 混合检索 / 问答）
react_agent.py       Function Calling Agent（工具注册表 / 多步循环 / 流式事件）
store.py             SQLite 文档库（知识库 / 文档 / 版本 / 片段 / 配置）
vector_store.py      Chroma 向量存储封装
ingestion.py         文档解析（PDF/Word/Markdown/TXT，含本地 OCR 兜底）与切分
utils/retrieval.py   分词 / BM25 / RRF / MMR 工具
utils/logger.py      线程安全的 JSONL Agent 轨迹日志
evals/               golden set 评测（离线/在线两种模式）
tests/               46 个离线测试（mock LLM 与 Embedding）
.github/workflows/   CI（pytest + ruff + mypy）
docs/                架构说明与学习指南
```

## 数据存储

- 元数据与片段：`data/kb.sqlite3`（SQLite，WAL 模式，可被后台线程安全访问）；
- 向量：`data/chroma/`（Chroma，每个知识库一个 collection）；
- 文档：`data/docs/<kb_id>/<doc_id>/v<版本>/<文件名>`（版本化保存）；
- Agent 轨迹：`data/agent_trace.jsonl`。
- 页面 Logo：`assets/logo.png`（河南辉煌科技官网下载，可通过 `LOGO_PATH` 替换）。

## 隐私与安全提示

- OCR 在本机执行，但 Embedding 与问答会把文档片段发送给阿里云 DashScope；
  请只上传已获授权用于该云服务的文档。
- Streamlit 页面仍为单机演示（无鉴权）；**FastAPI 服务层支持 Bearer Token 鉴权**。
  多人使用请部署在内网并开启 `API_AUTH_TOKEN`，完整 RBAC 属阶段 B 规划。

## 与旧版（MVP）的主要区别

| 维度 | 旧版 | 阶段 A |
| --- | --- | --- |
| 知识库 | 单一，JSON manifest | 多知识库，SQLite 存储 |
| 检索 | 纯向量 Top-5 | 向量 + BM25 混合，RRF + MMR + 阈值 |
| Agent | 关键词路由，单次调用 | Function Calling 选工具，多步循环 |
| 入库 | 同步阻塞 UI | 后台线程异步索引 |
| 版本 | 同名覆盖 | 自动升版本，旧版保留 |
| 查询 | 无法处理追问题 | 多轮查询改写 |
| 幻觉防护 | 无 | 严格接地 Prompt + 来源限定检索（问题提到人名/文件名时只检索该文档） |
| 测试 | 无 | 46 项离线测试 + ruff + mypy + CI |

## 下一步（阶段 B / C）

架构说明见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，
从零学习路线见 [docs/LEARNING_GUIDE.md](docs/LEARNING_GUIDE.md)。
阶段 A 已完成：FastAPI 服务层（基础 Bearer 鉴权）、golden set 评测脚本。
阶段 B 计划：用户/租户与 RBAC、PostgreSQL、任务队列、观测与成本统计；
阶段 C 计划：MCP 工具接入公司系统、人工审批、模型网关。
