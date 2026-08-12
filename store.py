"""SQLite 知识库/文档元数据存储。

阶段 A 用 SQLite 替代原来单个 JSON manifest，原因：

- 支持多知识库、文档分类/标签、文件版本；
- 支持按文档删除、按知识库查询，数据一致性好；
- WAL 模式 + busy_timeout，可被后台入库线程和前台问答线程安全访问；
- 单文件、零部署，适合作为平台阶段 A 的存储底座，阶段 B 可平滑迁移到 PostgreSQL。

表结构说明：

- ``kbs``：知识库（名称唯一）；
- ``documents``：文档主记录（同一文件名重传时版本递增）；
- ``document_versions``：文件版本历史（每个版本的 SHA-256、大小、物理路径）；
- ``chunks``：文本片段（content + 元数据 JSON），供 BM25 重建与检索使用；
- ``meta``：键值配置，例如“索引配置版本号”，用于触发全量重建。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kbs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    kb_id TEXT NOT NULL REFERENCES kbs(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    latest_version INTEGER NOT NULL DEFAULT 1,
    uploaded_at TEXT NOT NULL,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    indexed_sha TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS document_versions (
    id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    file_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(doc_id, version)
);

CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    kb_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    metadata TEXT NOT NULL,
    UNIQUE(doc_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Kb:
    id: str
    name: str
    description: str
    created_at: str


@dataclass
class DocumentInfo:
    id: str
    kb_id: str
    filename: str
    category: str
    tags: str
    status: str
    latest_version: int
    uploaded_at: str
    chunk_count: int
    indexed_sha: str
    file_path: str
    sha256: str
    size: int


@dataclass
class Chunk:
    id: str
    doc_id: str
    kb_id: str
    chunk_index: int
    content: str
    metadata: dict[str, Any]


class DocumentStore:
    """SQLite 封装：每个公开方法独立开连接，写事务用锁保护。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        '''
        RLock 是线程锁（可重入锁）。回忆上一轮讲的：这个项目里后台入库线程在写文档、前台问答线程在读片段，两个线程可能同时操作数据库。这把锁的作用就是保证同一时刻只有一个线程在执行写操作。
        "可重入"是指：同一个线程拿着锁的时候，再次申请同一把锁不会被自己卡死。这里就有一个真实案例——create_document 方法内部拿着锁调用了 get_document，如果不是可重入锁（RLock）而是普通锁（Lock），程序就死锁了。
        '''
        self._lock = RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                self._ensure_columns(conn)
                conn.commit()
            finally:
                conn.close()

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        """旧库升级：给 documents 补 indexed_sha 列（已索引内容哈希）。"""
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(documents)")}
        if "indexed_sha" not in columns:
            conn.execute("ALTER TABLE documents ADD COLUMN indexed_sha TEXT NOT NULL DEFAULT ''")

    # ---------- 知识库 ----------

    def create_kb(self, name: str, description: str = "", kb_id: str | None = None) -> Kb:
        """创建知识库；名称重复时抛 ValueError。kb_id 仅在迁移默认库时使用。"""
        with self._lock:
            conn = self._connect()
            try:
                try:
                    conn.execute(
                        "INSERT INTO kbs (id, name, description, created_at) VALUES (?, ?, ?, ?)",
                        (kb_id or _new_id(), name.strip(), description.strip(), _now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ValueError(f"知识库「{name}」已存在") from exc
                conn.commit()
                row = conn.execute("SELECT * FROM kbs WHERE name = ?", (name.strip(),)).fetchone()
                return Kb(**dict(row))
            finally:
                conn.close()

    def list_kbs(self) -> list[Kb]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM kbs ORDER BY created_at").fetchall()
            return [Kb(**dict(row)) for row in rows]
        finally:
            conn.close()

    def get_kb(self, kb_id: str) -> Kb | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM kbs WHERE id = ?", (kb_id,)).fetchone()
            return Kb(**dict(row)) if row else None
        finally:
            conn.close()

    def get_kb_by_name(self, name: str) -> Kb | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM kbs WHERE name = ?", (name.strip(),)).fetchone()
            return Kb(**dict(row)) if row else None
        finally:
            conn.close()

    def delete_kb(self, kb_id: str) -> None:
        """删除知识库及其文档、片段记录（物理文件由上层清理）。"""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM chunks WHERE kb_id = ?", (kb_id,))
                conn.execute("DELETE FROM documents WHERE kb_id = ?", (kb_id,))
                conn.execute("DELETE FROM kbs WHERE id = ?", (kb_id,))
                conn.commit()
            finally:
                conn.close()

    # ---------- 文档与版本 ----------

    def create_document(
        self,
        kb_id: str,
        filename: str,
        category: str = "",
        tags: str = "",
    ) -> DocumentInfo:
        """创建文档主记录（版本 1）。"""
        with self._lock:
            conn = self._connect()
            try:
                doc_id = _new_id()
                conn.execute(
                    "INSERT INTO documents (id, kb_id, filename, category, tags, status, latest_version, uploaded_at) "
                    "VALUES (?, ?, ?, ?, ?, 'active', 1, ?)",
                    (doc_id, kb_id, filename, category.strip(), tags.strip(), _now()),
                )
                conn.commit()
                return self.get_document(doc_id)  # type: ignore[return-value]
            finally:
                conn.close()

    def bump_document(self, doc_id: str, category: str = "", tags: str = "") -> DocumentInfo:
        """同一文件重传：版本 +1，更新分类/标签与上传时间。"""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
                if row is None:
                    raise KeyError(f"文档不存在：{doc_id}")
                version = int(row["latest_version"]) + 1
                conn.execute(
                    "UPDATE documents SET latest_version = ?, category = ?, tags = ?, "
                    "uploaded_at = ?, status = 'active' WHERE id = ?",
                    (version, category.strip(), tags.strip(), _now(), doc_id),
                )
                conn.commit()
                return self.get_document(doc_id)  # type: ignore[return-value]
            finally:
                conn.close()

    def add_version(
        self,
        doc_id: str,
        version: int,
        sha256: str,
        size: int,
        file_path: str | Path,
    ) -> None:
        """登记一个文件版本（物理文件由上层先写入磁盘）。"""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO document_versions (id, doc_id, version, sha256, size, file_path, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (_new_id(), doc_id, version, sha256, size, str(file_path), _now()),
                )
                conn.commit()
            finally:
                conn.close()

    def get_document(self, doc_id: str) -> DocumentInfo | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT d.*, COALESCE(v.sha256, '') AS sha256, "
                "COALESCE(v.size, 0) AS size, COALESCE(v.file_path, '') AS file_path "
                "FROM documents d "
                "LEFT JOIN document_versions v ON v.doc_id = d.id AND v.version = d.latest_version "
                "WHERE d.id = ?",
                (doc_id,),
            ).fetchone()
            return self._doc_from_row(row) if row else None
        finally:
            conn.close()

    def find_document_by_name(self, kb_id: str, filename: str) -> DocumentInfo | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT d.*, COALESCE(v.sha256, '') AS sha256, "
                "COALESCE(v.size, 0) AS size, COALESCE(v.file_path, '') AS file_path "
                "FROM documents d "
                "LEFT JOIN document_versions v ON v.doc_id = d.id AND v.version = d.latest_version "
                "WHERE d.kb_id = ? AND d.filename = ? AND d.status = 'active'",
                (kb_id, filename),
            ).fetchone()
            return self._doc_from_row(row) if row else None
        finally:
            conn.close()

    def list_documents(self, kb_id: str, include_deleted: bool = False) -> list[DocumentInfo]:
        conn = self._connect()
        try:
            status_filter = "" if include_deleted else "AND d.status = 'active'"
            rows = conn.execute(
                "SELECT d.*, COALESCE(v.sha256, '') AS sha256, "
                "COALESCE(v.size, 0) AS size, COALESCE(v.file_path, '') AS file_path "
                "FROM documents d "
                "LEFT JOIN document_versions v ON v.doc_id = d.id AND v.version = d.latest_version "
                "WHERE d.kb_id = ? " + status_filter + " ORDER BY d.uploaded_at",
                (kb_id,),
            ).fetchall()
            return [self._doc_from_row(row) for row in rows]
        finally:
            conn.close()

    def delete_document(self, doc_id: str) -> DocumentInfo | None:
        """软删除文档并清空其片段记录；文件与版本历史保留（便于审计/恢复）。"""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
                if row is None:
                    return None
                conn.execute("UPDATE documents SET status = 'deleted' WHERE id = ?", (doc_id,))
                conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
                conn.commit()
                return self.get_document(doc_id)
            finally:
                conn.close()

    def set_indexed_sha(self, doc_id: str, sha256: str) -> None:
        """入库成功后记录“已索引内容哈希”，避免下次重复入库。"""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("UPDATE documents SET indexed_sha = ? WHERE id = ?", (sha256, doc_id))
                conn.commit()
            finally:
                conn.close()

    def set_chunk_count(self, doc_id: str, count: int) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("UPDATE documents SET chunk_count = ? WHERE id = ?", (count, doc_id))
                conn.commit()
            finally:
                conn.close()

    def reset_chunk_counts(self, kb_id: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("UPDATE documents SET chunk_count = 0 WHERE kb_id = ?", (kb_id,))
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _doc_from_row(row: sqlite3.Row) -> DocumentInfo:
        data = dict(row)
        return DocumentInfo(
            id=data["id"],
            kb_id=data["kb_id"],
            filename=data["filename"],
            category=data["category"],
            tags=data["tags"],
            status=data["status"],
            latest_version=int(data["latest_version"]),
            uploaded_at=data["uploaded_at"],
            chunk_count=int(data["chunk_count"]),
            indexed_sha=data.get("indexed_sha", ""),
            file_path=data["file_path"],
            sha256=data["sha256"],
            size=int(data["size"]),
        )

    # ---------- 片段 ----------

    def replace_chunks(self, chunks: list[Chunk]) -> None:
        """整体替换某文档的片段（先删旧再插新，保证与向量库一致）。"""
        if not chunks:
            return
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM chunks WHERE doc_id = ?", (chunks[0].doc_id,))
                conn.executemany(
                    "INSERT INTO chunks (id, doc_id, kb_id, chunk_index, content, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            chunk.id,
                            chunk.doc_id,
                            chunk.kb_id,
                            chunk.chunk_index,
                            chunk.content,
                            json.dumps(chunk.metadata, ensure_ascii=False),
                        )
                        for chunk in chunks
                    ],
                )
                conn.commit()
            finally:
                conn.close()

    def delete_chunks(self, doc_ids: list[str] | None = None, kb_id: str | None = None) -> None:
        with self._lock:
            conn = self._connect()
            try:
                if doc_ids:
                    conn.executemany("DELETE FROM chunks WHERE doc_id = ?", [(did,) for did in doc_ids])
                if kb_id:
                    conn.execute("DELETE FROM chunks WHERE kb_id = ?", (kb_id,))
                conn.commit()
            finally:
                conn.close()

    def iter_chunks(self, kb_id: str) -> Iterator[Chunk]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM chunks WHERE kb_id = ? ORDER BY doc_id, chunk_index",
                (kb_id,),
            )
            for row in rows:
                yield Chunk(
                    id=row["id"],
                    doc_id=row["doc_id"],
                    kb_id=row["kb_id"],
                    chunk_index=int(row["chunk_index"]),
                    content=row["content"],
                    metadata=json.loads(row["metadata"]),
                )
        finally:
            conn.close()

    def count_chunks(self, kb_id: str) -> int:
        conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) AS n FROM chunks WHERE kb_id = ?", (kb_id,)).fetchone()
            return int(row["n"])
        finally:
            conn.close()

    # ---------- 键值配置 ----------

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
                conn.commit()
            finally:
                conn.close()

    def get_meta(self, key: str) -> str | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None
        finally:
            conn.close()


def main() -> None:
    """手动测试入口：走一遍核心流程，验证各方法可用。

    直接运行本文件即可：python store.py
    数据库建在临时目录，跑完自动删除，不会污染项目。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "demo_store.sqlite3"
        store = DocumentStore(db_path)
        print(f"数据库文件：{db_path}")

        # 1. 创建知识库
        kb = store.create_kb("产品手册", description="产品相关文档")
        print(f"创建知识库：{kb.name}（id={kb.id[:8]}...）")
        print(f"知识库列表：{[k.name for k in store.list_kbs()]}")

        # 2. 创建文档主记录 + 登记文件版本
        doc = store.create_document(kb.id, "报销制度.txt", category="制度", tags="财务")
        store.add_version(doc.id, 1, sha256="abc123", size=1024, file_path=Path(tmp) / "报销制度.txt")
        print(f"创建文档：{doc.filename} v{doc.latest_version}")

        # 3. 模拟同名重传：版本 +1
        found = store.find_document_by_name(kb.id, "报销制度.txt")
        assert found is not None, "重传前应已存在同名文档"
        bumped = store.bump_document(found.id, category="制度", tags="财务,报销")
        store.add_version(bumped.id, 2, sha256="def456", size=2048, file_path=Path(tmp) / "报销制度_v2.txt")
        print(f"重传后版本：v{bumped.latest_version}")

        # 4. 写入文本片段
        chunks = [
            Chunk(id=_new_id(), doc_id=doc.id, kb_id=kb.id, chunk_index=0,
                  content="差旅报销需在结束后 7 天内提交。", metadata={"source": "报销制度.txt"}),
            Chunk(id=_new_id(), doc_id=doc.id, kb_id=kb.id, chunk_index=1,
                  content="市内交通费每日上限 200 元。", metadata={"source": "报销制度.txt"}),
        ]
        store.replace_chunks(chunks)
        print(f"片段数量：{store.count_chunks(kb.id)}")
        for c in store.iter_chunks(kb.id):
            print(f"  [{c.chunk_index}] {c.content}")

        # 5. 键值配置
        store.set_meta("index_version", "3")
        print(f"meta 读取：index_version={store.get_meta('index_version')}")

        # 6. 软删除文档：片段应被清空
        store.delete_document(doc.id)
        print(f"删除文档后片段数量：{store.count_chunks(kb.id)}")
        print(f"文档列表（含已删除）：{[(d.filename, d.status) for d in store.list_documents(kb.id, include_deleted=True)]}")

        print("全部流程执行完毕 ✅")


if __name__ == "__main__":
    main()
