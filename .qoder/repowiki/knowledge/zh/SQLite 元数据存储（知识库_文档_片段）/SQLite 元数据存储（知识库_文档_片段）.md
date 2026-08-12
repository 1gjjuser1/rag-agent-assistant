---
kind: external_dependency
name: SQLite 元数据存储（知识库/文档/片段）
slug: sqlite
category: external_dependency
category_hints:
    - migration_status
    - client_constraint
scope:
    - '**'
---

阶段 A 用 SQLite 替代原 JSON manifest，承载知识库、文档主记录、版本历史、文本片段和键值配置。

- 文件位置：`data/kb.sqlite3`，启用 WAL 模式与 `busy_timeout=10000`，允许后台入库线程与前台问答线程并发访问。
- 表结构：`kbs`（知识库）、`documents`（文档主记录，含 `indexed_sha` 防重复入库）、`document_versions`（SHA-256 与物理路径）、`chunks`（片段内容+JSON 元数据）、`meta`（配置项）。
- 版本管理：同名文件重传时 `latest_version` 自增，旧版本保留在磁盘便于审计恢复。
- 迁移策略：单文件零部署，阶段 B 计划平滑迁移到 PostgreSQL。