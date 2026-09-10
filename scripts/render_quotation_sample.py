"""Render a golden quotation fixture to PDF (+ PNG) for visual comparison.

    .venv/bin/python scripts/render_quotation_sample.py [navapakam|happy-table] [out.pdf]

Defaults: navapakam → /tmp/nav.pdf and /tmp/nav.png (page 1 at 150 dpi).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.quotation_pdf import render_sample  # noqa: E402
from app.services.quotation_raster import pdf_page_count, pdf_page_to_png  # noqa: E402

name = sys.argv[1] if len(sys.argv) > 1 else "navapakam"
out = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/nav.pdf")
pdf = render_sample(name)
out.write_bytes(pdf)
out.with_suffix(".png").write_bytes(pdf_page_to_png(pdf, dpi=150))
print(f"{name}: {len(pdf)} bytes, {pdf_page_count(pdf)} page(s) → {out} / {out.with_suffix('.png')}")
