"""Phase 2: reference numbering + the issue transaction + read endpoints."""
import copy
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.services.quotation_fixtures import NAVAPAKAM_DRAFT
from app.services.quotation_service import financial_year, format_reference


def test_financial_year_boundaries():
    assert financial_year(date(2026, 4, 1)) == "2026-27"
    assert financial_year(date(2026, 9, 11)) == "2026-27"
    assert financial_year(date(2027, 3, 31)) == "2026-27"
    assert financial_year(date(2027, 4, 1)) == "2027-28"
    assert financial_year(date(2099, 12, 1)) == "2099-00"


def test_format_reference():
    assert format_reference(date(2026, 9, 14), "SW", 49) == "1409SW049/2026-27"
    assert format_reference(date(2027, 1, 5), "SW", 1000) == "0501SW1000/2026-27"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """App with an in-memory SQLite DB, local storage in tmp_path, admin user."""
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_UPLOAD_DIR", str(tmp_path / "uploads"))

    from app.database import Base
    from app.main import app
    from app.models import quotation, user  # noqa: F401 — register tables
    from app.services.auth import get_current_user
    from app.database import get_db
    from app.services.storage import reset_storage_cache

    from app.config import get_settings
    get_settings.cache_clear()  # settings are lru_cached — pick up the env overrides
    reset_storage_cache()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def _db():
        db = Session()
        try:
            yield db
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    class _U:
        id = 1
        role = "ADMIN"
        active = True
        username = "admin-test"

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: _U()
    try:
        yield TestClient(app), Session, tmp_path
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
        reset_storage_cache()
        get_settings.cache_clear()


def _draft(**over):
    d = copy.deepcopy(NAVAPAKAM_DRAFT)
    d["reference"] = None
    d.update(over)
    return d


def test_next_reference_is_a_peek_and_does_not_increment(client):
    c, _, _ = client
    r = c.get("/api/v1/admin/quotations/next-reference?date=2026-07-14")
    assert r.status_code == 200, r.text
    assert r.json() == {"reference": "1407SW001/2026-27", "fy": "2026-27", "next_number": 1, "date": "2026-07-14"}
    r2 = c.get("/api/v1/admin/quotations/next-reference?date=2026-07-15")
    assert r2.json()["reference"] == "1507SW001/2026-27"


def test_signatories_and_seed_products(client):
    c, _, _ = client
    sigs = c.get("/api/v1/admin/quotations/signatories").json()
    assert sigs[0]["name"] == "SRINIVAS NARAYAN" and sigs[0]["initials"] == "SW"
    # The catalogue is DB-backed (Phase 4): seeded at startup, empty until then.
    assert c.get("/api/v1/admin/quotations/products").json() == []
    from tests.test_quotation_catalogue import _seed
    _seed(c)
    prods = c.get("/api/v1/admin/quotations/products").json()
    assert len(prods) == 3 and prods[0]["image_asset"] == "sk-pos-m95-touch-pos.png"
    assert c.get("/api/v1/admin/quotations/products?q=s200e").json()[0]["model"] == "S200E"


def test_create_assigns_reference_stores_pdf_and_inserts_rows(client):
    c, Session, tmp_path = client
    r = c.post("/api/v1/admin/quotations", json=_draft())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["reference"] == "1407SW001/2026-27"
    assert body["status"] == "ISSUED"
    assert body["grand_total"] == "58374.60" and body["subtotal"] == "49470.00"
    assert body["signatory_name"] == "SRINIVAS NARAYAN"
    assert body["address_lines"] == NAVAPAKAM_DRAFT["address_lines"]
    assert len(body["items"]) == 4
    assert body["items"][0]["image_asset"] == "sk-pos-m95-touch-pos.png"
    assert body["items"][3]["line_total"] == "870.00"
    assert body["pdf_url"].endswith("/uploads/quotations/1407SW001-2026-27/1407SW001-2026-27.pdf")

    stored = tmp_path / "uploads" / "quotations" / "1407SW001-2026-27" / "1407SW001-2026-27.pdf"
    assert stored.is_file() and stored.read_bytes().startswith(b"%PDF")

    # Sequence advanced; next number is 002 and a new FY restarts at 001.
    assert c.get("/api/v1/admin/quotations/next-reference?date=2026-07-14").json()["next_number"] == 2
    r2 = c.post("/api/v1/admin/quotations", json=_draft(quotation_date="2027-04-01"))
    assert r2.status_code == 201 and r2.json()["reference"] == "0104SW001/2027-28"
    r3 = c.post("/api/v1/admin/quotations", json=_draft())
    assert r3.json()["reference"] == "1407SW002/2026-27"

    # Detail + download.
    qid = body["id"]
    det = c.get(f"/api/v1/admin/quotations/{qid}")
    assert det.status_code == 200 and det.json()["reference"] == "1407SW001/2026-27"
    f = c.get(f"/api/v1/admin/quotations/{qid}/file")
    assert f.status_code == 200
    assert f.headers["content-type"] == "application/pdf"
    assert f.headers["content-disposition"] == 'attachment; filename="1407SW001-2026-27.pdf"'
    assert f.content.startswith(b"%PDF")
    assert c.get("/api/v1/admin/quotations/999").status_code == 404

    # List.
    lst = c.get("/api/v1/admin/quotations?q=navapakam").json()
    assert lst["total"] == 3 and lst["items"][0]["customer_name"] == "NAVAPAKAM KITCHENS LLP"
    assert c.get("/api/v1/admin/quotations?q=nobody").json()["total"] == 0
    assert c.get("/api/v1/admin/quotations?from=2027-01-01").json()["total"] == 1


def test_manual_reference_is_kept_and_duplicates_are_409(client):
    c, _, _ = client
    r = c.post("/api/v1/admin/quotations", json=_draft(reference="14072920/2026-27"))
    assert r.status_code == 201 and r.json()["reference"] == "14072920/2026-27"
    dup = c.post("/api/v1/admin/quotations", json=_draft(reference="14072920/2026-27"))
    assert dup.status_code == 409
    assert "already used" in dup.json()["detail"]
    # A manual reference does not consume an auto number.
    assert c.get("/api/v1/admin/quotations/next-reference?date=2026-07-14").json()["next_number"] == 1
    # ...and the auto allocator skips over a hand-typed value equal to its next one.
    c.post("/api/v1/admin/quotations", json=_draft(reference="1407SW001/2026-27"))
    auto = c.post("/api/v1/admin/quotations", json=_draft())
    assert auto.status_code == 201 and auto.json()["reference"] == "1407SW002/2026-27"


def test_storage_failure_inserts_nothing(client, monkeypatch):
    c, Session, _ = client
    from app.services import storage as storage_mod

    class _Boom(storage_mod.LocalStorage):
        def save_bytes(self, *a, **k):
            raise RuntimeError("bucket down")

    monkeypatch.setattr("app.services.storage.get_storage", lambda: _Boom())
    with pytest.raises(RuntimeError):
        c.post("/api/v1/admin/quotations", json=_draft())
    from app.models.quotation import Quotation, QuotationSequence
    with Session() as s:
        assert s.query(Quotation).count() == 0
        assert s.query(QuotationSequence).count() == 0  # the allocation rolled back too
    assert c.get("/api/v1/admin/quotations/next-reference?date=2026-07-14").json()["next_number"] == 1


@pytest.mark.parametrize("role", ["ENGINEER"])
def test_quotations_are_closed_to_engineers(client, role):
    """Managers and Sales reps are let in; Engineers aren't."""
    c, _, _ = client
    from app.main import app
    from app.services.auth import get_current_user

    class _U2:
        id = 2
        active = True
        username = "other-test"

    _U2.role = role
    app.dependency_overrides[get_current_user] = lambda: _U2()
    assert c.post("/api/v1/admin/quotations", json=_draft()).status_code == 403
    assert c.get("/api/v1/admin/quotations").status_code == 403
    assert c.get("/api/v1/admin/quotations/next-reference").status_code == 403


def test_presets(client):
    c, _, _ = client
    r = c.get("/api/v1/admin/quotations/presets")
    assert r.status_code == 200
    body = r.json()
    assert body["terms"]["POS"][0] == "Quotation validity for {days} Days"
    assert body["notes"]["CCTV"]["style"] == "RED_TEXT"
    assert body["totals_labels"]["SIMPLE"]["grand_total"] == "GRAND TOTAL"


# --------------------------- Phase 3: formats + duplicate ---------------- #

def _issue(c, **over):
    r = c.post("/api/v1/admin/quotations", json=_draft(**over))
    assert r.status_code == 201, r.text
    return r.json()


def test_file_formats_render_once_and_cache(client, monkeypatch):
    c, Session, tmp_path = client
    q = _issue(c)
    qid, base = q["id"], f"/api/v1/admin/quotations/{q['id']}/file"

    import app.services.quotation_docx as docx_mod
    calls = {"docx": 0}
    real = docx_mod.render_quotation_docx

    def counting(model):
        calls["docx"] += 1
        return real(model)

    monkeypatch.setattr(docx_mod, "render_quotation_docx", counting)

    r = c.get(f"{base}?format=docx")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert r.headers["content-disposition"] == 'attachment; filename="1407SW001-2026-27.docx"'
    assert r.content[:2] == b"PK"
    assert calls["docx"] == 1
    assert (tmp_path / "uploads" / "quotations" / "1407SW001-2026-27" / "1407SW001-2026-27.docx").is_file()

    # Second download is served from the cache — no re-render.
    r2 = c.get(f"{base}?format=docx")
    assert r2.status_code == 200 and r2.content == r.content
    assert calls["docx"] == 1
    from app.models.quotation import Quotation
    with Session() as s:
        row = s.get(Quotation, qid)
        assert row.docx_storage_key and row.docx_storage_key.endswith(".docx")
        assert row.png_storage_key is None

    png = c.get(f"{base}?format=png")
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    assert png.headers["content-disposition"] == 'attachment; filename="1407SW001-2026-27.png"'
    assert png.content[:8] == b"\x89PNG\r\n\x1a\n"

    jpg = c.get(f"{base}?format=jpeg")
    assert jpg.status_code == 200 and jpg.headers["content-type"] == "image/jpeg"
    assert jpg.headers["content-disposition"] == 'attachment; filename="1407SW001-2026-27.jpg"'
    assert jpg.content[:3] == b"\xff\xd8\xff"

    # 200 dpi A4 → 1654 × 2339 px.
    from PIL import Image
    import io
    im = Image.open(io.BytesIO(jpg.content))
    assert im.size == (1654, 2339) and im.mode == "RGB"

    pdf = c.get(f"{base}?format=pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert pdf.headers["content-disposition"] == 'attachment; filename="1407SW001-2026-27.pdf"'
    assert c.get(f"{base}?format=doc").status_code == 422

    with Session() as s:
        row = s.get(Quotation, qid)
        assert row.png_storage_key.endswith(".png") and row.jpeg_storage_key.endswith(".jpg")


def test_duplicate_returns_a_valid_draft_that_reissues(client):
    c, _, _ = client
    src = _issue(c, reference="14072920/2026-27")
    r = c.post(f"/api/v1/admin/quotations/{src['id']}/duplicate")
    assert r.status_code == 200, r.text
    draft = r.json()
    assert draft["reference"] is None
    assert draft["quotation_date"] == date.today().isoformat()
    assert draft["duplicated_from_id"] == src["id"]
    assert draft["customer_name"] == "NAVAPAKAM KITCHENS LLP"
    assert draft["subject_line"] == "For Sarjapur Road outlet"
    assert [i["headline"] for i in draft["items"]] == [i["headline"] for i in NAVAPAKAM_DRAFT["items"]]
    assert draft["items"][0]["image_asset"] == "sk-pos-m95-touch-pos.png"
    assert draft["items"][0]["include_image"] is True
    assert draft["items"][0]["model"] == "Mighty Series\nM95"
    assert draft["terms"] == src["terms"]

    # The draft is a valid payload: preview works and issue assigns a fresh number.
    assert c.post("/api/v1/admin/quotations/preview", json=draft).status_code == 200
    issued = c.post("/api/v1/admin/quotations", json=draft)
    assert issued.status_code == 201, issued.text
    body = issued.json()
    assert body["reference"] != src["reference"]
    assert body["reference"].endswith(f"/{'2026-27' if date.today() < date(2027, 4, 1) else '2027-28'}")
    assert body["duplicated_from_id"] == src["id"]
    assert body["grand_total"] == src["grand_total"]
    assert c.post("/api/v1/admin/quotations/999/duplicate").status_code == 404


def test_managers_can_use_the_quotation_workflow(client):
    """Managers were admin-only until the gate was split: they now get the whole
    quotation workflow (issue / read / download / duplicate)."""
    c, _, _ = client
    q = _issue(c)
    from app.main import app
    from app.services.auth import get_current_user

    class _M:
        id = 2
        role = "MANAGER"
        active = True
        username = "manager-test"

    app.dependency_overrides[get_current_user] = lambda: _M()
    assert c.get(f"/api/v1/admin/quotations/{q['id']}/file?format=docx").status_code == 200
    assert c.post(f"/api/v1/admin/quotations/{q['id']}/duplicate").status_code == 200
    assert c.post("/api/v1/admin/quotations", json=_draft()).status_code == 201
    assert c.get("/api/v1/admin/quotations").status_code == 200
    assert c.get(f"/api/v1/admin/quotations/{q['id']}").status_code == 200
    assert c.get("/api/v1/admin/quotations/products").status_code == 200
    assert c.get("/api/v1/admin/quotations/next-reference").status_code == 200


def test_sales_reps_can_use_the_quotation_workflow_but_not_edit_the_catalogue(client):
    """Sales reps get the whole quotation workflow (issue / read / download /
    duplicate / edit) and can read the catalogue, but can't change it."""
    c, _, _ = client
    q = _issue(c)
    from app.main import app
    from app.services.auth import get_current_user

    class _S:
        id = 2
        role = "SALES"
        active = True
        username = "sales-test"

    app.dependency_overrides[get_current_user] = lambda: _S()
    assert c.get(f"/api/v1/admin/quotations/{q['id']}/file?format=docx").status_code == 200
    assert c.post(f"/api/v1/admin/quotations/{q['id']}/duplicate").status_code == 200
    assert c.post("/api/v1/admin/quotations", json=_draft()).status_code == 201
    assert c.get("/api/v1/admin/quotations").status_code == 200
    assert c.get(f"/api/v1/admin/quotations/{q['id']}").status_code == 200
    draft = c.get(f"/api/v1/admin/quotations/{q['id']}/draft").json()
    assert c.put(f"/api/v1/admin/quotations/{q['id']}", json=draft).status_code == 200
    assert c.get("/api/v1/admin/quotations/products").status_code == 200
    assert c.get("/api/v1/admin/quotations/next-reference").status_code == 200
    assert c.post("/api/v1/admin/quotations/products",
                  data={"payload": '{"name": "M", "headline": "H"}'}).status_code == 403
    assert c.delete("/api/v1/admin/quotations/products/1").status_code == 403


def test_edit_keeps_the_reference_and_rerenders(client):
    """An edit corrects the row in place: same id, same reference, new totals,
    and the superseded alternate renders are dropped so they regenerate."""
    c, Session, _ = client
    q = _issue(c)
    # Cache a docx so we can prove the edit invalidates it.
    assert c.get(f"/api/v1/admin/quotations/{q['id']}/file?format=docx").status_code == 200

    from app.models.quotation import Quotation
    with Session() as db:
        before = db.get(Quotation, q["id"])
        assert before.docx_storage_key is not None
        assert before.updated_at is None

    draft = c.get(f"/api/v1/admin/quotations/{q['id']}/draft").json()
    assert draft["reference"] == q["reference"]  # not cleared, unlike duplicate
    draft["customer_name"] = "Edited Customer Pvt Ltd"
    draft["items"][0]["quantity"] = "4"

    r = c.put(f"/api/v1/admin/quotations/{q['id']}", json=draft)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == q["id"]
    assert body["reference"] == q["reference"]
    assert body["customer_name"] == "Edited Customer Pvt Ltd"
    assert body["grand_total"] != q["grand_total"]
    assert body["updated_at"] is not None

    with Session() as db:
        after = db.get(Quotation, q["id"])
        assert after.docx_storage_key is None  # superseded render dropped
        assert after.png_storage_key is None
        assert after.pdf_storage_key is not None
        assert [i.quantity for i in after.items][0] == 4
        # Items are replaced, not appended to.
        assert len(after.items) == len(draft["items"])


def test_edit_ignores_a_reference_change(client):
    """The reference identifies the document the customer already holds, so a
    draft carrying a different one must not move it."""
    c, _, _ = client
    q = _issue(c)
    draft = c.get(f"/api/v1/admin/quotations/{q['id']}/draft").json()
    draft["reference"] = "9999ZZ999/2099-00"
    r = c.put(f"/api/v1/admin/quotations/{q['id']}", json=draft)
    assert r.status_code == 200, r.text
    assert r.json()["reference"] == q["reference"]


def test_edit_404s_for_an_unknown_quotation(client):
    c, _, _ = client
    q = _issue(c)
    draft = c.get(f"/api/v1/admin/quotations/{q['id']}/draft").json()
    assert c.put("/api/v1/admin/quotations/9999", json=draft).status_code == 404
    assert c.get("/api/v1/admin/quotations/9999/draft").status_code == 404


def test_managers_can_edit_and_manage_the_catalogue(client):
    """Quotation management is Manager-level now, catalogue included."""
    c, _, _ = client
    q = _issue(c)
    from app.main import app
    from app.services.auth import get_current_user

    class _M:
        id = 2
        role = "MANAGER"
        active = True
        username = "manager-test"

    app.dependency_overrides[get_current_user] = lambda: _M()
    draft = c.get(f"/api/v1/admin/quotations/{q['id']}/draft").json()
    assert c.put(f"/api/v1/admin/quotations/{q['id']}", json=draft).status_code == 200
    assert c.post("/api/v1/admin/quotations/products",
                  data={"payload": '{"name": "M", "headline": "H"}'}).status_code == 201
