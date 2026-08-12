"""文档解析与切分测试：乱码检测阈值、页码归一化。"""

from __future__ import annotations

from langchain_core.documents import Document

from ingestion import PDF_TEXT_QUALITY_THRESHOLD, DocumentParser, build_chunk_metadata


def test_text_quality_clean_chinese() -> None:
    text = "李明，北京石油化工学院人工智能研究院硕士，2021.09-2024.06。"
    assert DocumentParser._text_quality(text) >= PDF_TEXT_QUALITY_THRESHOLD


def test_text_quality_detects_cid_garbled() -> None:
    # pypdf 对 CID 字体 PDF 的典型乱码：中文变成 exotic 字符，ASCII 部分完好。
    garbled = "຦૓ঊ\ng19137935578 · Ⴏདtest@example.com\nྟљgଳ ·م·"
    assert DocumentParser._text_quality(garbled) < PDF_TEXT_QUALITY_THRESHOLD


def test_text_quality_empty() -> None:
    assert DocumentParser._text_quality("  \n\t ") == 0.0


def test_build_chunk_metadata_page_is_one_based() -> None:
    doc = Document(page_content="内容", metadata={"source": "简历.pdf", "page": 0})
    meta = build_chunk_metadata(doc, "简历.pdf", 1, "doc1", "kb1")
    assert meta["page"] == 1
    assert meta["paragraph"] == 1

    doc2 = Document(page_content="内容", metadata={"source": "简历.pdf", "page": 1})
    assert build_chunk_metadata(doc2, "简历.pdf", 2, "doc1", "kb1")["page"] == 2


def test_pdf_per_page_ocr_fallback(tmp_path, monkeypatch) -> None:
    """混合型 PDF：文本页直接用，空白/扫描页单独走 OCR，不再整本丢弃。"""
    import pymupdf

    pdf_path = tmp_path / "mixed.pdf"
    doc = pymupdf.open()
    text_page = doc.new_page()
    text_page.insert_text((72, 72), "This is a readable text page.")
    doc.new_page()  # 无文本层的空白页
    doc.save(pdf_path)
    doc.close()

    ocr_pages: list[int] = []

    def fake_ocr_page(page, source, page_number, engine=None) -> str:
        ocr_pages.append(page_number)
        return "OCR识别出的文字"

    monkeypatch.setattr(
        DocumentParser, "_ocr_page", staticmethod(fake_ocr_page)
    )
    parser = DocumentParser()
    docs = parser._load_pdf(pdf_path)

    assert len(docs) == 2
    assert docs[0].page_content.strip() == "This is a readable text page."
    assert docs[1].page_content == "OCR识别出的文字"
    assert docs[1].metadata["page"] == 1
    assert ocr_pages == [1]
