---
kind: external_dependency
name: 阿里云 DashScope（百炼）大模型与 Embedding 服务
slug: aliyun-dashscope
category: external_dependency
category_hints:
    - vendor_identity
    - auth_protocol
    - sdk_real_api
scope:
    - '**'
---

本项目通过 OpenAI 兼容接口调用阿里云 DashScope（百炼），作为唯一的 LLM 与 Embedding 供应商。

- 认证：从环境变量 `DASHSCOPE_API_KEY` 读取密钥，未配置时抛出明确错误；`base_url` 默认指向公共兼容端点 `https://dashscope.aliyuncs.com/compatible-mode/v1`，可通过 `.env` 覆盖为私有 MaaS 业务空间地址。
- 对话：使用 `openai.OpenAI` 客户端以 `chat.completions.create` 调用，支持同步、流式、Function Calling（`tools`/`tool_choice`）以及联网搜索（`enable_search=True`）。
- Embedding：通过 LangChain `langchain_community.embeddings.DashScopeEmbeddings` 获取向量，供 ChromaDB 使用。
- 模型名：默认对话模型 `qwen-plus`、嵌入模型 `text-embedding-v3`，均可由 `DASHSCOPE_CHAT_MODEL` / `DASHSCOPE_EMBEDDING_MODEL` 覆盖。
- 思考过程：通过 `extra_body.enable_thinking` 控制是否返回 reasoning_content，流式输出中会先产出 reasoning 再产出 content。