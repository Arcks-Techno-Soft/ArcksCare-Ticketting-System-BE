"""PDF → PNG/JPEG via PyMuPDF (for phone previews and WhatsApp downloads).

Downloads render page 1 only for now (multi-page quotations are rare); the
preview endpoint uses a lower dpi for speed.
"""
from __future__ import annotations

import fitz  # PyMuPDF

DOWNLOAD_DPI = 200
JPEG_QUALITY = 92


def _pixmap(pdf_bytes: bytes, page: int, dpi: int):
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        if page >= doc.page_count:
            page = doc.page_count - 1
        # alpha=False flattens onto white, so JPEGs get a white background.
        return doc[page].get_pixmap(dpi=dpi, alpha=False)


def pdf_page_to_png(pdf_bytes: bytes, *, page: int = 0, dpi: int = 150) -> bytes:
    return _pixmap(pdf_bytes, page, dpi).tobytes("png")


def pdf_page_to_jpeg(pdf_bytes: bytes, *, page: int = 0, dpi: int = DOWNLOAD_DPI, quality: int = JPEG_QUALITY) -> bytes:
    return _pixmap(pdf_bytes, page, dpi).tobytes("jpeg", jpg_quality=quality)


def pdf_page_count(pdf_bytes: bytes) -> int:
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        return doc.page_count
