"""PDF → PNG/JPEG via PyMuPDF (for phone previews and WhatsApp downloads)."""
from __future__ import annotations

import fitz  # PyMuPDF


def pdf_page_to_png(pdf_bytes: bytes, *, page: int = 0, dpi: int = 150) -> bytes:
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        if page >= doc.page_count:
            page = doc.page_count - 1
        pix = doc[page].get_pixmap(dpi=dpi, alpha=False)
        return pix.tobytes("png")


def pdf_page_count(pdf_bytes: bytes) -> int:
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        return doc.page_count
