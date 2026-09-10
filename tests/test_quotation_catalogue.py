"""Phase 4: DB-backed catalogue, image uploads, seeding, retire-safety."""
import copy
import io

import pytest
from PIL import Image

from app.services.quotation_fixtures import NAVAPAKAM_DRAFT

# Reuse the app/DB/storage fixture from the issue tests.
from tests.test_quotation_issue import client  # noqa: F401

ROOT = "/api/v1/admin/quotations"


def _png(w=1200, h=900, mode="RGB"):
    buf = io.BytesIO()
    Image.new(mode, (w, h), (200, 30, 30) if mode == "RGB" else (200, 30, 30, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _seed(c):
    from app.services.quotation_catalogue import seed_quotation_products
    from app.main import app
    from app.database import get_db
    gen = app.dependency_overrides[get_db]()
    db = next(gen)
    try:
        return seed_quotation_products(db)
    finally:
        db.close()


def test_seeding_is_idempotent_across_two_startups(client):
    c, Session, _ = client
    assert _seed(c) == 3
    assert _seed(c) == 0
    rows = c.get(f"{ROOT}/products").json()
    assert [r["name"] for r in rows] == [
        "SK-POS M95 Touch POS System", "STOUT S200E Thermal Printer", "SK-POS SKC582 Cash Drawer",
    ]
    assert rows[0]["image_asset"] == "sk-pos-m95-touch-pos.png"
    assert rows[0]["image_url"] == "/static/quotation-products/sk-pos-m95-touch-pos.png"
    assert all(r["active"] for r in rows) and [r["sort_order"] for r in rows] == [0, 1, 2]
    assert c.get(f"{ROOT}/products?q=s200e").json()[0]["model"] == "S200E"


def test_product_crud_with_image(client):
    c, _, tmp_path = client
    payload = {"name": "TEST_ Barcode Scanner", "brand": "ZEBRA", "model": "DS2208",
               "headline": "2D [[Barcode]] Scanner", "spec_lines": "USB\nAuto sense", "warranty_label": "1 Year",
               "default_unit_price": "5400", "default_row_style": "DETAILED"}
    r = c.post(f"{ROOT}/products", data={"payload": __import__("json").dumps(payload)},
               files={"image": ("scanner.png", _png(), "image/png")})
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["name"] == "TEST_ Barcode Scanner" and p["default_unit_price"] == "5400.00"
    assert p["image_storage_key"].endswith("/quotation-products/%d/product.jpg" % p["id"]) or \
        p["image_storage_key"] == f"quotation-products/{p['id']}/product.jpg"
    assert p["image_url"].startswith("/uploads/quotation-products/")
    stored = tmp_path / "uploads" / "quotation-products" / str(p["id"]) / "product.jpg"
    assert stored.is_file()
    im = Image.open(stored)
    assert max(im.size) == 800 and im.format == "JPEG"  # re-encoded + downsized

    # PATCH text + remove image; then upload a transparent PNG (stays PNG).
    r = c.patch(f"{ROOT}/products/{p['id']}", data={"payload": '{"name": "TEST_ Scanner v2", "remove_image": true}'})
    assert r.status_code == 200 and r.json()["name"] == "TEST_ Scanner v2" and r.json()["image_url"] is None
    r = c.patch(f"{ROOT}/products/{p['id']}", data={"payload": "{}"},
                files={"image": ("s.png", _png(300, 300, "RGBA"), "image/png")})
    assert r.status_code == 200 and r.json()["image_storage_key"].endswith("product.png")

    # Reorder: new product first.
    ids = [x["id"] for x in c.get(f"{ROOT}/products?active=all").json()]
    rows = c.post(f"{ROOT}/products/reorder", json={"ids": [p["id"]] + [i for i in ids if i != p["id"]]}).json()
    assert rows[0]["id"] == p["id"] and rows[0]["sort_order"] == 0
    assert c.post(f"{ROOT}/products/reorder", json={"ids": [999]}).status_code == 404

    # Soft delete: gone from the default list, visible with active=all / false.
    assert c.delete(f"{ROOT}/products/{p['id']}").status_code == 204
    assert p["id"] not in [x["id"] for x in c.get(f"{ROOT}/products").json()]
    assert p["id"] in [x["id"] for x in c.get(f"{ROOT}/products?active=false").json()]
    assert c.get(f"{ROOT}/products?active=all").json()[0]["active"] is False
    assert c.delete(f"{ROOT}/products/999").status_code == 404
    assert c.patch(f"{ROOT}/products/999", data={"payload": "{}"}).status_code == 404


def test_non_image_upload_is_422(client):
    c, _, _ = client
    r = c.post(f"{ROOT}/item-images", files={"image": ("notes.txt", b"hello", "text/plain")})
    assert r.status_code == 422
    # Right content type, garbage bytes → still 422 (Pillow can't decode it).
    r = c.post(f"{ROOT}/item-images", files={"image": ("x.png", b"not really a png", "image/png")})
    assert r.status_code == 422
    r = c.post(f"{ROOT}/products", data={"payload": '{"name":"TEST_ X","headline":"x"}'},
               files={"image": ("doc.pdf", b"%PDF-1.4", "application/pdf")})
    assert r.status_code == 422
    r = c.post(f"{ROOT}/products", data={"payload": '{"name":"","headline":"x"}'})
    assert r.status_code == 422


def test_item_image_upload_round_trips_into_a_quotation(client):
    c, _, tmp_path = client
    r = c.post(f"{ROOT}/item-images", files={"image": ("pic.png", _png(640, 480), "image/png")})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["storage_key"].startswith("/uploads/quotation-items/") and body["url"] == body["storage_key"]
    draft = copy.deepcopy(NAVAPAKAM_DRAFT)
    draft["reference"] = None
    draft["items"] = [dict(draft["items"][3], row_style="DETAILED", include_image=True,
                           image_storage_key=body["storage_key"], spec_lines="Colour : Black")]
    assert c.post(f"{ROOT}/preview", json=draft).status_code == 200
    issued = c.post(ROOT, json=draft)
    assert issued.status_code == 201, issued.text
    assert issued.json()["items"][0]["image_storage_key"] == body["storage_key"]


def test_issued_quotation_renders_after_product_is_retired(client):
    c, _, _ = client
    _seed(c)
    m95 = c.get(f"{ROOT}/products").json()[0]
    draft = copy.deepcopy(NAVAPAKAM_DRAFT)
    draft["reference"] = None
    # Row references the product only — no asset/key of its own.
    draft["items"] = [dict(draft["items"][0], product_id=m95["id"], image_asset=None, image_storage_key=None, include_image=True)]
    assert c.post(f"{ROOT}/preview", json=draft).status_code == 200
    q = c.post(ROOT, json=draft).json()
    # The picture source was snapshotted onto the row at issue time.
    assert q["items"][0]["image_asset"] == "sk-pos-m95-touch-pos.png"

    assert c.delete(f"{ROOT}/products/{m95['id']}").status_code == 204
    for fmt in ("pdf", "docx", "png"):
        r = c.get(f"{ROOT}/{q['id']}/file?format={fmt}")
        assert r.status_code == 200, fmt
    dup = c.post(f"{ROOT}/{q['id']}/duplicate").json()
    assert dup["items"][0]["image_asset"] == "sk-pos-m95-touch-pos.png"
    assert c.post(ROOT, json=dup).status_code == 201


@pytest.mark.parametrize("method,path,kwargs", [
    ("get", "/products", {}),
    ("post", "/products", {"data": {"payload": "{}"}}),
    ("patch", "/products/1", {"data": {"payload": "{}"}}),
    ("delete", "/products/1", {}),
    ("post", "/products/reorder", {"json": {"ids": [1]}}),
    ("post", "/item-images", {"files": {"image": ("a.png", b"x", "image/png")}}),
])
def test_catalogue_routes_are_admin_only(client, method, path, kwargs):
    c, _, _ = client
    from app.main import app
    from app.services.auth import get_current_user

    class _M:
        id = 2
        role = "MANAGER"
        active = True

    app.dependency_overrides[get_current_user] = lambda: _M()
    assert getattr(c, method)(f"{ROOT}{path}", **kwargs).status_code == 403
