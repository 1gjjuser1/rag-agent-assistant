# 学习指南：从零看懂这个项目

这份指南假设你熟悉 Python 基础，但不熟悉 RAG 和 Agent。按顺序读，每个概念都
对应到本项目代码里的具体位置，读完你就能改这个项目。

## 推荐阅读顺序

1. **先跑起来**：按 README 启动 `streamlit run app.py`，上传一个文档，问它几个问题；
2. **读 `README.md`**：了解功能与配置；
3. **读 `docs/ARCHITECTURE.md`**：了解整体结构与数据流；
4. **按下面的小节逐个看代码**，边看边在 Streamlit 里改参数观察效果；
5. **改完跑测试**：`pytest`，确保没改坏。

## 概念 1：RAG 是什么

RAG（Retrieval-Augmented Generation，检索增强生成）= **先检索，再生成**。

大模型只会“记得”训练时见过的知识，公司内部文档它没见过。RAG 的思路是：

```text
文档 → 切分成片段 → 每个片段转成向量（Embedding）→ 存进向量库
问题 → 转成向量 → 在向量库里找最像的 Top-K 片段 → 把片段和问题一起给大模型 → 大模型基于片段回答
```

对应代码：入库在 `rag_pipeline.py::ingest`，检索在 `::retrieve`，生成在 `::answer`。

## 概念 2：Embedding 与向量检索

Embedding 是把文本变成一串数字（向量）的模型。语义相近的文本，向量也相近
（余弦相似度高）。比如“设备报警”和“系统告警”用词不同，但向量距离很近。

本项目用阿里云 `text-embedding-v3`（1024 维），封装在 `llm_client.py`；
向量库是 Chroma（`vector_store.py`），每个知识库一个 collection，距离度量是余弦。

**动手实验**：把 `.env` 里 `RETRIEVAL_TOP_K` 改成 1 再提问，回答会明显变“单薄”，
你就直观感受到 Top-K 的作用。

## 概念 3：BM25 与混合检索

向量检索擅长“语义”，但不擅长精确术语。BM25 是经典的词面算法：问题里的词在
文档里出现越多、这个词越稀有，得分越高。它不依赖模型，完全可解释。

混合检索 = 向量 + BM25 各自取 Top-N，再用 **RRF** 把两份排名合并：

```text
RRF 得分 = Σ 1/(60 + 排名)
```

只看排名不看得分，所以两个检索器得分尺度不同也没关系。代码在
`utils/retrieval.py`（`reciprocal_rank_fusion`），融合逻辑在
`rag_pipeline.py::retrieve`。

**动手实验**：把 `RETRIEVAL_FUSION_POOL` 改成 3，检索结果会变少，观察引用的变化。

## 概念 4：MMR 去重

Top-K 片段经常是同一段的复述，浪费上下文。MMR（Maximal Marginal Relevance）
每轮挑选：

```text
得分 = λ × 与问题的相似度 − (1−λ) × 与已选片段的最大相似度
```

λ 越大越看重相关性，越小越看重多样性。代码在 `utils/retrieval.py::mmr_rerank`。

**动手实验**：`RETRIEVAL_MMR_ENABLED=false` 和 `true` 各问一次，对比引用片段的重复度。

## 概念 5：相关性阈值

如果问题跟知识库完全无关，检索也会硬返回 Top-5，模型就可能“编”答案。
本项目加了一道门槛：候选片段中最高的向量相似度低于
`RETRIEVAL_RELEVANCE_THRESHOLD`（默认 0.3）时，直接回答“没有检索到相关内容”。

**动手实验**：问一个与知识库完全无关的问题（如“火星上有水吗”），看返回；
再把阈值改成 0.9，问一个正常问题，感受“误杀”。

## 概念 6：查询改写

多轮对话里用户会说“那它呢？”“这个多少钱”，直接检索是查不到的。
查询改写 = 先用大模型把追问题补全成独立问题，再去检索。

```text
用户：介绍一下道岔监测系统
助手：好的……
用户：它有哪些组成？ → 改写为：道岔监测系统有哪些组成？
```

代码在 `rag_pipeline.py::rewrite_query`，由 `QUERY_REWRITE_ENABLED` 控制。

## 概念 7：Function Calling 与 Agent

Function Calling 让大模型不只是“说话”，还能“调用工具”：我们给模型一份工具清单
（名字、描述、参数 Schema），模型返回“我要调用 get_weather，参数是 {city: 北京}”。

Agent 循环（ReAct：Reason + Act）：

```text
第 1 轮：模型说“调用 search_knowledge_base(query=...)”
         → 我们执行，把结果以 role=tool 回填
第 2 轮：模型基于结果生成最终回答 → 结束
```

代码在 `react_agent.py`：

- `ToolRegistry`：工具注册表，注册新工具只加一个函数 + Schema；
- `run_stream`：循环主体，产出 `tool_start` / `tool_end` / `token` / `done` 事件；
- `_parse_arguments`：解析模型返回的 JSON 参数。

**动手实验**：在 `_build_registry` 里注册一个新工具（比如查内部 API），
重启后在界面上输入相关问题，看模型是否学会调用它。

## 概念 8：增量入库与索引配置版本

每次入库时计算文件 SHA-256，与上次入库时记录的 `indexed_sha` 比较：

- 没变 → 跳过（不重复 Embedding，省钱）；
- 变了 → 只重新索引这一个文件；
- 配置变了（chunk 大小或 Embedding 模型）→ 全部重建，因为旧向量无法与新配置混用。

注意区分两个哈希：`document_versions.sha256`（上传内容）与
`documents.indexed_sha`（已索引内容）——这是本项目修过的一个真实 bug，
理解了它你就理解了增量入库的本质。

## 概念 9：测试怎么写的

真实 LLM 和 Embedding 需要网络和花钱，所以测试用假的：

- `tests/helpers.py::FakeEmbeddings`：n-gram 词袋向量，语义相近的文本分数高；
- `tests/helpers.py::FakeLLMClient`：按“剧本”返回结果，可以模拟
  “先调用工具再回答”的完整流程。

这样 `pytest` 30 项测试可以离线、快速、确定性地跑完。

## 常见问题（FAQ）

**Q：为什么我上传后立刻提问，回答说没有相关内容？**
后台索引还在跑（侧边栏有进度），等“入库完成”再问；或者检查阈值是否过高。

**Q：为什么 Agent 不调用知识库工具，直接回答了？**
模型自己决定是否用工具；描述写得越清楚（见 `_build_registry` 里
`search_knowledge_base` 的描述），模型越倾向于调用。

**Q：我们的模型上下文有多长？会不会上下文太长导致回答变差？**
qwen3.7-max 官方上下文长度是 100 万 tokens（最大输入 991,808 / 最大输出
65,536），本应用单次请求实际只发送约 3000 tokens，远不会超窗。但“窗口大”
不等于“注意力不散”——长历史、长检索片段、长工具结果都会稀释模型对
关键信息的注意力。项目已用 `HISTORY_MAX_TOKENS`（2000）和
`RAG_CONTEXT_MAX_TOKENS`（2500）做预算截断，并限制工具返回长度。

**Q：修改了 chunk 参数为什么不生效？**
改 `.env` 后重启应用（Streamlit 默认要按 R），索引会在下次入库时自动全量重建。

**Q：多人同时用会怎样？**
阶段 A 没有用户体系，所有会话共享同一批知识库；阶段 B 会加租户与权限。

**Q：文档发到云端安全吗？**
Embedding 和问答都会把片段发送给 DashScope。涉密文档请等阶段 C 的私有化模型选项。
