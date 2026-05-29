"""
口腔医学考研带背系统 - PDF解析模块 v3
使用 PyMuPDF 提取文本，RapidOCR 处理扫描版 PDF
"""
import re
import os
import math
import tempfile

import fitz  # PyMuPDF

# OCR 懒加载（首次使用时初始化）
_ocr_engine = None


def _get_ocr():
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr_engine = RapidOCR()
    return _ocr_engine


def extract_text_from_pdf(pdf_path: str, progress_cb=None) -> str:
    """
    提取 PDF 全部文本（自动处理扫描版）
    progress_cb: 可选回调 progress_cb(current_page, total_pages)
    """
    doc = fitz.open(pdf_path)
    full_text = []
    has_text = False
    has_images = False

    for page in doc:
        text = page.get_text().strip()
        if text:
            has_text = True
            full_text.append(text)
        if page.get_images():
            has_images = True
    doc.close()

    # 如果提取到正常文字，直接返回
    if has_text and len("\n".join(full_text).replace("·", "").replace(".", "").strip()) > 20:
        return "\n".join(full_text)

    # 扫描版 PDF：用 OCR
    if has_images:
        return _ocr_extract(pdf_path, progress_cb)

    return "\n".join(full_text)


def _ocr_extract(pdf_path: str, progress_cb=None) -> str:
    """对扫描版 PDF 进行 OCR 识别"""
    ocr = _get_ocr()
    doc = fitz.open(pdf_path)
    all_text = []
    total_pages = len(doc)

    # 限制 OCR 页数（避免超时）
    max_pages = min(total_pages, 100)

    for i in range(max_pages):
        page = doc[i]
        pix = page.get_pixmap(dpi=150)  # 150 DPI 够用且快
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            pix.save(tmp.name)
            result, _ = ocr(tmp.name)
            os.unlink(tmp.name)
        if result:
            page_text = "\n".join(line[1] for line in result)
            all_text.append(page_text)
        # 回调进度
        if progress_cb:
            progress_cb(i + 1, total_pages)

    doc.close()
    return "\n".join(all_text)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        text = extract_text_from_pdf(sys.argv[1])
        print(f"提取文字：{len(text)} 字符")
        print(text[:500])
