"""Renderer + preview-route tests for quotations (Phase 1)."""
import copy

import fitz
import pytest
from fastapi.testclient import TestClient

from app.services.quotation_fixtures import HAPPY_TABLE_DRAFT, NAVAPAKAM_DRAFT
from app.services.quotation_pdf import render_quotation_pdf, render_sample
from app.services.quotation_service import build_render_model_from_dict


def _text(pdf: bytes) -> str:
    with fitz.open(stream=pdf, filetype="pdf") as doc:
        return "\n".join(p.get_text() for p in doc)


def _pages(pdf: bytes) -> int:
    with fitz.open(stream=pdf, filetype="pdf") as doc:
        return doc.page_count


def test_navapakam_fixture_is_one_page_with_exact_totals():
    pdf = render_sample("navapakam")
    assert pdf.startswith(b"%PDF")
    assert _pages(pdf) == 1
    text = _text(pdf)
    for needle in (
        "NAVAPAKAM KITCHENS LLP", "For Sarjapur Road outlet", "14072920/2026-27",
        "TOTAL BASIC PRICE", "49,470.00", "8,904.60", "58,374.60",
        "Note : 24/7 x 365 Onsite Service Support", "3 Years Onsite Warranty",
        "BANK DETAILS", "SRINIVAS NARAYAN", "www.sktechnosys.in",
        "Taxes : GST @ 18% OR AS APPLICABLE AT THE TIME OF INVOICE",
    ):
        assert needle in text, needle
    assert "Page 1 of" not in text  # no page numbers on single-page documents
    assert len(pdf) < 1_000_000  # images are downscaled before embedding


def test_happy_table_fixture_compact_layout():
    pdf = render_sample("happy-table")
    assert _pages(pdf) == 1
    text = _text(pdf)
    for needle in ("GRAND TOTAL", "1,10,940.00", "19,969.20", "1,30,909.20",
                   "Note: Cabling and Monitor not added in the above Prices",
                   "Fright : Included", "Sl"):
        assert needle in text, needle


def test_red_markup_renders_red_and_is_escaped():
    draft = copy.deepcopy(NAVAPAKAM_DRAFT)
    draft["items"] = [dict(draft["items"][3], headline="A <b>&</b> [[RED PART]] item")]
    pdf = render_quotation_pdf(build_render_model_from_dict(draft))
    with fitz.open(stream=pdf, filetype="pdf") as doc:
        spans = [s for b in doc[0].get_text("dict")["blocks"] if b["type"] == 0
                 for l in b["lines"] for s in l["spans"]]
    red = [s["text"] for s in spans if s["color"] == 0xFF0000]
    assert any("RED PART" in t for t in red)
    text = "\n".join(s["text"] for s in spans)
    assert "<b>&</b>" in text  # literal, not interpreted as markup


def test_many_detailed_items_paginate_with_repeated_header_and_page_numbers():
    draft = copy.deepcopy(NAVAPAKAM_DRAFT)
    draft["items"] = [copy.deepcopy(draft["items"][0]) for _ in range(8)]
    pdf = render_quotation_pdf(build_render_model_from_dict(draft))
    n = _pages(pdf)
    assert n >= 2
    with fitz.open(stream=pdf, filetype="pdf") as doc:
        for i, page in enumerate(doc):
            t = page.get_text()
            assert "PRODUCT NAME & DESCRIPTION" in t  # header row repeats
            if i == 0:
                assert "QUOTATION" in t and "Page 1 of" not in t
            else:
                assert f"Page {i + 1} of {n}" in t
                assert "Bill To" not in t  # header block on page 1 only
        last = doc[-1].get_text()
        assert "OFFICE :" in last and "TOTAL BASIC PRICE" in last


# --------------------------- route / role gate --------------------------- #

@pytest.fixture
def client():
    from app.main import app
    from app.services.auth import get_current_user

    def _make(role):
        class _U:
            id = 1
            role = None
            active = True
        u = _U(); u.role = role
        return u

    state = {"role": "ADMIN"}
    app.dependency_overrides[get_current_user] = lambda: _make(state["role"])
    try:
        yield TestClient(app), state  # no `with` → startup (DB bootstrap) never runs
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_preview_returns_pdf_and_totals_headers(client):
    c, _ = client
    r = c.post("/api/v1/admin/quotations/preview", json=NAVAPAKAM_DRAFT)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")
    assert r.headers["x-subtotal"] == "49470.00"
    assert r.headers["x-gst"] == "8904.60"
    assert r.headers["x-grand-total"] == "58374.60"


def test_preview_png_format(client):
    c, _ = client
    r = c.post("/api/v1/admin/quotations/preview?format=png", json=HAPPY_TABLE_DRAFT)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.parametrize("role", ["MANAGER", "ENGINEER", "SALES"])
def test_preview_is_admin_only(client, role):
    c, state = client
    state["role"] = role
    r = c.post("/api/v1/admin/quotations/preview", json=NAVAPAKAM_DRAFT)
    assert r.status_code == 403


@pytest.mark.parametrize("role", ["SUPER_ADMIN", "OWNER"])
def test_preview_allows_super_admin_and_legacy_owner(client, role):
    c, state = client
    state["role"] = role
    r = c.post("/api/v1/admin/quotations/preview", json=NAVAPAKAM_DRAFT)
    assert r.status_code == 200


def test_preview_validation_errors(client):
    c, _ = client
    bad = copy.deepcopy(NAVAPAKAM_DRAFT)
    bad["customer_gstin"] = "NOTAGSTIN"
    assert c.post("/api/v1/admin/quotations/preview", json=bad).status_code == 422
    bad = copy.deepcopy(NAVAPAKAM_DRAFT)
    bad["items"] = []
    assert c.post("/api/v1/admin/quotations/preview", json=bad).status_code == 422
    bad = copy.deepcopy(NAVAPAKAM_DRAFT)
    bad["items"][0]["image_storage_key"] = "../etc/passwd"
    assert c.post("/api/v1/admin/quotations/preview", json=bad).status_code == 422
    bad = copy.deepcopy(NAVAPAKAM_DRAFT)
    bad["items"][0]["image_asset"] = "../../.env"
    assert c.post("/api/v1/admin/quotations/preview", json=bad).status_code == 422


# --------------------------- DOCX (Phase 3/4) ---------------------------- #

def _soffice():
    import shutil
    for cand in (shutil.which("soffice"), "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        if cand and __import__("os").path.exists(cand):
            return cand
    return None


@pytest.mark.parametrize("name", ["navapakam", "happy-table"])
def test_docx_renders_and_fits_one_page_under_font_substitution(name, tmp_path):
    """The Word copy must stay a single page even where Lucida Fax / Calibri
    are missing (LibreOffice substitutes wider fonts) — the trailing
    paragraphs Word needs around nested tables used to push the footer over."""
    import subprocess

    from app.services.quotation_docx import render_quotation_docx
    from app.services.quotation_fixtures import FIXTURES
    from app.services.quotation_service import build_render_model_from_dict

    data = render_quotation_docx(build_render_model_from_dict(FIXTURES[name]))
    assert data[:2] == b"PK"
    soffice = _soffice()
    if not soffice:
        pytest.skip("LibreOffice not installed — cannot rasterise DOCX")
    src = tmp_path / f"{name}.docx"
    src.write_bytes(data)
    subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", str(tmp_path), str(src)],
                   check=True, capture_output=True, timeout=180)
    pdf = (tmp_path / f"{name}.pdf").read_bytes()
    assert _pages(pdf) == 1
    text = _text(pdf)
    assert "OFFICE :" in text and "BANK DETAILS" in text
