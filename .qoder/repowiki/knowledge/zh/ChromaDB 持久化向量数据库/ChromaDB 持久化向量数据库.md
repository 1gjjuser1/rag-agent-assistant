---
kind: external_dependency
name: ChromaDB 持久化向量数据库
slug: chromadb
category: external_dependency
category_hints:
    - framework_behavior
    - client_constraint
scope:
    - '**'
---

项目直接使用 `chromadb.PersistentClient` 而非 LangChain 封装，以便精确控制批量 embedding、按 `doc_id` 删除、取回原始向量做 MMR 去重。

- 存储位置：`data/chroma/`，每个知识库对应一个 collection，命名形如 `kb_<kb_id>`。
- 距离度量：collection 创建时指定 `hnsw:space=cosine`，查询结果中的 distance 需转换为相似度 `1 - distance`。
- 写入：按 `batch_size`（默认 32）分批调用 `embed_documents` 后 `upsert`，id 相同则覆盖，天然支持增量更新。
- 查询：`query` 支持 `where` 条件过滤（用于限定某文档检索），并返回 documents/metadatas/distances。
- 生命周期：提供 `clear_collection`、`list_collections`、`reset`（重建客户端连接以避免内存索引过期）等能力。
- 约束：Chroma 为单机嵌入式向量库，适合 MVP/单机部署；阶段 B 计划迁移至 PostgreSQL + 外部向量引擎。