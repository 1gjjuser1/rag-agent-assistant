"""文档解析与切分：把原始文件变成可入库的文本片段。

支持 PDF / Word(docx) / Markdown / TXT。PDF 先做文本提取，检测到乱码或
扫描件时自动切换为本地 OCR（PyMuPDF + RapidOCR），不上传云端。
"""

from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any

from langchain_community.document_loaders import Docx2txtLoader, PyPDFLoader, TextLoader
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

SUPPORTED_SUFFIXES = {".pdf", ".md", ".txt", ".docx"}
# PDF 文本可读性阈值：低于该值视为乱码/扫描件，自动切换到本地 OCR。
# 实测 pypdf 对部分 CID 字体 PDF 会产出“ASCII 正常 + 中文乱码”的混合文本，
# 质量分约 0.71；干净中文 PDF 通常 ≥ 0.93，因此取 0.85 作为分界。
PDF_TEXT_QUALITY_THRESHOLD = 0.85


class DocumentParser:
    """按后缀加载文件；支持注入替换（测试时可用假解析器）。"""

    def load(self, path: Path) -> list[Document]:
        suffix = path.suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            raise ValueError(f"不支持的文件类型：{suffix}")
        if suffix == ".pdf":
            return self._load_pdf(path)
        if suffix == ".docx":
            return Docx2txtLoader(str(path)).load()
        if suffix == ".md":
            # 不依赖 unstructured 重依赖，直接用文本加载并保留原文。
            return TextLoader(str(path), encoding="utf-8").load()
        return TextLoader(str(path), encoding="utf-8", autodetect_encoding=True).load()

    def _load_pdf(self, path: Path) -> list[Document]:
        """按页处理 PDF：文本层可读的页直接使用，空页/乱码页单独走 OCR。

        旧实现把整本 PDF 拼起来判断质量，混合型文档（部分页文本、部分页
        扫描）会被整体判为“可读”，扫描页静默丢失；现在逐页判断更准确。
        """
        try:
            import pymupdf
        except ImportError:
            # 缺少 PyMuPDF 时回退到 pypdf 提取 + 整本质量判断。
            fallback_docs = PyPDFLoader(str(path)).load()
            combined = "\n".join(doc.page_content for doc in fallback_docs)
            if self._text_quality(combined) < PDF_TEXT_QUALITY_THRESHOLD:
                return self._ocr_pdf(path)
            return fallback_docs

        documents: list[Document] = []
        with pymupdf.open(path) as pdf:
            for page_index, page in enumerate(pdf):
                text = page.get_text().strip()
                if text and self._text_quality(text) >= PDF_TEXT_QUALITY_THRESHOLD:
                    documents.append(
                        Document(
                            page_content=text,
                            metadata={"source": path.name, "page": page_index},
                        )
                    )
                    continue
                ocr_text = self._ocr_page(page, path.name, page_index)
                if ocr_text:
                    documents.append(
                        Document(
                            page_content=ocr_text,
                            metadata={"source": path.name, "page": page_index},
                        )
                    )
        if not documents:
            raise RuntimeError(f"未能从 {path.name} 提取或识别出文字。")
        return documents

    @staticmethod
    def _extract_pdf_text(path: Path) -> list[Document]:
        """用 PyMuPDF 提取文本层，按页返回（page 为 0 起编号）。

        PyMuPDF 对中文 CID 字体的还原能力强于 pypdf；若环境缺少 pymupdf，
        回退到 pypdf，仍由质量检测决定是否走 OCR。
        """
        try:
            import pymupdf
        except ImportError:
            return PyPDFLoader(str(path)).load()
        documents: list[Document] = []
        with pymupdf.open(path) as pdf:
            for page_index, page in enumerate(pdf):
                text = page.get_text().strip()
                if text:
                    documents.append(
                        Document(
                            page_content=text,
                            metadata={"source": path.name, "page": page_index},
                        )
                    )
        return documents

    @staticmethod
    def _text_quality(text: str) -> float:
        """估算提取文本是否可读，识别 PDF 自定义字体映射造成的乱码。"""
        visible = [char for char in text if not char.isspace()]
        if not visible:
            return 0.0
        readable = 0
        for char in visible:
            name = unicodedata.name(char, "")
            if (
                "\u4e00" <= char <= "\u9fff"
                or char.isascii()
                or any(
                    marker in name
                    for marker in ("LATIN", "DIGIT", "PUNCTUATION", "HIRAGANA", "KATAKANA")
                )
            ):
                readable += 1
        return readable / len(visible)

    @staticmethod
    def _ocr_pdf(path: Path) -> list[Document]:
        """使用本地 PyMuPDF + RapidOCR 对乱码或扫描 PDF 逐页 OCR。"""
        try:
            import pymupdf
            from rapidocr import RapidOCR
        except ImportError as exc:
            raise RuntimeError(
                f"PDF 文本不可读且本地 OCR 依赖未安装。请执行 "
                f"`pip install pymupdf rapidocr onnxruntime`。原始错误：{exc}"
            ) from exc

        engine = RapidOCR()
        documents: list[Document] = []
        with pymupdf.open(path) as pdf:
            for page_number, page in enumerate(pdf, start=0):
                text = DocumentParser._ocr_page(page, path.name, page_number, engine)
                if text:
                    documents.append(
                        Document(
                            page_content=text,
                            metadata={"source": path.name, "page": page_number},
                        )
                    )
        if not documents:
            raise RuntimeError(f"本地 OCR 未能从 {path.name} 识别出文字。")
        return documents

    @staticmethod
    def _ocr_page(
        page: Any,
        source: str,
        page_number: int,
        engine: Any | None = None,
    ) -> str:
        """对单页渲染 PNG 并 OCR，返回识别文本（无内容时返回空串）。"""
        try:
            import pymupdf

            if engine is None:
                from rapidocr import RapidOCR

                engine = RapidOCR()
            # 2 倍分辨率兼顾小字号识别率与 CPU 耗时。
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
            result = engine(pixmap.tobytes("png"))
            lines = list(getattr(result, "txts", ()) or ())
            return "\n".join(str(line) for line in lines if str(line).strip())
        except Exception as exc:
            raise RuntimeError(
                f"第 {page_number + 1} 页 OCR 失败（{source}）：{exc}"
            ) from exc


def split_documents(
    documents: list[Document],
    chunk_size: int,
    chunk_overlap: int,
) -> list[Document]:
    """递归字符切分：优先按段落/句子边界切，保留 page 等元数据。"""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        add_start_index=True,
        separators=["\n\n", "\n", "。", "；", "，", " ", ""],
    )
    return splitter.split_documents(documents)


def build_chunk_metadata(
    source_doc: Document,
    filename: str,
    chunk_index: int,
    doc_id: str,
    kb_id: str,
    category: str = "",
    tags: str = "",
) -> dict[str, Any]:
    """生成片段元数据；source 用于引用，page/paragraph 用于定位。"""
    metadata: dict[str, Any] = {
        "source": filename,
        "chunk_index": chunk_index,
        "doc_id": doc_id,
        "kb_id": kb_id,
        "category": category,
        "tags": tags,
    }
    if source_doc.metadata.get("page") is not None:
        # 内部统一 0 起编号，对外展示为 1 起（与人类阅读习惯一致）。
        metadata["page"] = int(source_doc.metadata["page"]) + 1
    metadata["paragraph"] = chunk_index
    return metadata
