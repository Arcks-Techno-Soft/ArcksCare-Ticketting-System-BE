"""Spare catalog: the ops team's list, placeholder retirement, the per-product
picker (own parts + shared Accessories) and Admin-level editing."""
import pytest

from app.services.spares import (
    ACCESSORIES_CATEGORY,
    DEFAULT_CATALOG,
    RETIRED_PLACEHOLDERS,
    retire_placeholder_spares,
    seed_spare_catalog,
)

# Reuse the app/DB/storage fixture from the issue tests.
from tests.test_quotation_issue import client  # noqa: F401

ROOT = "/api/v1/admin/spare-catalog"


def _as(role):
    from app.main import app
    from app.services.auth import get_current_user

    class _U:
        id = 2
        active = True
        username = f"{role.lower()}-test"

    _U.role = role
    app.dependency_overrides[get_current_user] = lambda: _U()


def test_sheet_prices_are_stored_gst_inclusive():
    prices = {(p, n): price for p, n, price in DEFAULT_CATALOG}
    assert prices[("Printer", "Printer Motherboard")] == 3776  # 3200 + 18%
    assert prices[("Printer", "Printer Head")] == 3363         # 2850 + 18%
    assert prices[("Printer", "Printer Cutter")] == 3363
    assert prices[("Printer", "Printer Adaptor (24V)")] == 3776
    assert prices[("POS Machine", "POS Adaptor")] == 3776


def test_no_duplicate_catalog_rows_and_no_placeholder_reseeded():
    keys = [(p, n) for p, n, _ in DEFAULT_CATALOG]
    assert len(keys) == len(set(keys))
    assert not set(keys) & {(p, n) for p, n, _ in RETIRED_PLACEHOLDERS}


def test_retirement_hides_placeholders_but_spares_an_edited_row(client):
    _, Session, _ = client
    from app.models.spare import SpareCatalog
    with Session() as db:
        for p, n, price in RETIRED_PLACEHOLDERS:
            db.add(SpareCatalog(product_category=p, name=n, default_price_inr=price))
        # An admin repriced this one — it must survive.
        db.query(SpareCatalog).filter_by(name="Print head").first().default_price_inr = 1999
        db.commit()
        seed_spare_catalog(db)
        assert retire_placeholder_spares(db) == len(RETIRED_PLACEHOLDERS) - 1
        assert retire_placeholder_spares(db) == 0  # idempotent
        assert db.query(SpareCatalog).filter_by(name="Print head").one().active is True
        assert db.query(SpareCatalog).filter_by(name="Cutter blade").one().active is False


def test_picker_lists_product_parts_then_accessories(client):
    c, Session, _ = client
    with Session() as db:
        seed_spare_catalog(db)
    rows = c.get(f"{ROOT}?product=Printer").json()
    cats = [r["product_category"] for r in rows]
    assert set(cats) == {"Printer", ACCESSORIES_CATEGORY}
    # All of the product's own parts come before the shared accessories.
    assert cats == sorted(cats, key=lambda x: x == ACCESSORIES_CATEGORY)
    assert "Dell Mouse" in [r["name"] for r in rows]
    cash = [r["name"] for r in c.get(f"{ROOT}?product=Cash Drawer").json() if r["product_category"] == "Cash Drawer"]
    assert cash == ["Cash Drawer", "Cash Drawer Key Set"]


def test_admin_can_add_reprice_rename_and_retire(client):
    c, _, _ = client
    r = c.post(ROOT, json={"product_category": "Printer", "name": "Paper Sensor", "default_price_inr": 590})
    assert r.status_code == 201, r.text
    item = r.json()
    assert c.post(ROOT, json={"product_category": "Printer", "name": "paper sensor"}).status_code == 409
    r = c.patch(f"{ROOT}/{item['id']}", json={"default_price_inr": 649, "name": "Paper Sensor (Gap)"})
    assert r.status_code == 200 and r.json()["default_price_inr"] == 649
    assert c.patch(f"{ROOT}/{item['id']}", json={"active": False}).json()["active"] is False
    assert item["id"] not in [x["id"] for x in c.get(f"{ROOT}?product=Printer").json()]
    assert item["id"] in [x["id"] for x in c.get(f"{ROOT}?product=Printer&include_inactive=true").json()]
    assert c.patch(f"{ROOT}/999", json={"active": False}).status_code == 404


@pytest.mark.parametrize("role", ["MANAGER", "ENGINEER", "SALES"])
def test_catalog_editing_is_admin_level(client, role):
    c, _, _ = client
    _as(role)
    assert c.get(f"{ROOT}?product=Printer").status_code == 200  # everyone can read
    assert c.post(ROOT, json={"product_category": "Printer", "name": "X"}).status_code == 403
    assert c.patch(f"{ROOT}/1", json={"default_price_inr": 1}).status_code == 403


def test_renamed_or_retired_seed_parts_are_not_re_added_on_restart(client):
    """Seeding matches rows by seed_key, not by name — renaming a starter part
    in Settings must not bring the old name back on the next startup."""
    c, Session, _ = client
    from app.models.spare import SpareCatalog
    with Session() as db:
        seed_spare_catalog(db)
        total = db.query(SpareCatalog).count()
    cutter = next(r for r in c.get(f"{ROOT}?product=Printer").json() if r["name"] == "Printer Cutter")
    assert c.patch(f"{ROOT}/{cutter['id']}", json={"name": "Printer Auto Cutter"}).status_code == 200
    mouse = next(r for r in c.get(f"{ROOT}?product=Printer").json() if r["name"] == "Dell Mouse")
    assert c.patch(f"{ROOT}/{mouse['id']}", json={"active": False}).status_code == 200
    with Session() as db:
        assert seed_spare_catalog(db) == 0  # the "restart"
        assert db.query(SpareCatalog).count() == total
        assert db.query(SpareCatalog).filter_by(name="Printer Cutter").count() == 0


def test_legacy_rows_are_claimed_and_blid_becomes_blade(client):
    """Rows seeded before seed_key existed (NULL key), including the misspelt
    "Printer Blid", are claimed by name instead of duplicated."""
    _, Session, _ = client
    from app.models.spare import SpareCatalog
    with Session() as db:
        db.add(SpareCatalog(product_category="Printer", name="Printer Blid", default_price_inr=0))
        db.add(SpareCatalog(product_category="Printer", name="Printer Head", default_price_inr=3500))
        db.commit()
        added = seed_spare_catalog(db)
        assert added == len(DEFAULT_CATALOG) - 2
        assert db.query(SpareCatalog).filter_by(name="Printer Blid").count() == 0
        blade = db.query(SpareCatalog).filter_by(name="Printer Blade").one()
        assert blade.seed_key == "Printer|Printer Blade"
        head = db.query(SpareCatalog).filter_by(name="Printer Head").one()
        assert head.seed_key == "Printer|Printer Head" and head.default_price_inr == 3500  # price kept
        assert db.query(SpareCatalog).filter(SpareCatalog.seed_key.is_(None)).count() == 0


def test_seed_renames_move_the_seed_key_so_nothing_is_duplicated(client):
    """A corrected default name must claim the already-seeded row (which holds
    the OLD seed_key) rather than insert a second row on the next startup —
    and a row an admin renamed in the meantime keeps the admin's name."""
    _, Session, _ = client
    from app.models.spare import SpareCatalog
    with Session() as db:
        db.add(SpareCatalog(product_category="CCTV", name="Power adapter", default_price_inr=350,
                            seed_key="CCTV|Power adapter"))
        db.add(SpareCatalog(product_category="Kitchen Display Screen", name="KDS PSU", default_price_inr=900,
                            seed_key="Kitchen Display Screen|Power adapter"))
        db.commit()
        seed_spare_catalog(db)
        cctv = db.query(SpareCatalog).filter_by(product_category="CCTV").filter(
            SpareCatalog.name.ilike("power adapt%")).all()
        assert [(r.name, r.seed_key, r.default_price_inr) for r in cctv] == [
            ("Power adaptor", "CCTV|Power adaptor", 350)]
        kds = db.query(SpareCatalog).filter_by(seed_key="Kitchen Display Screen|Power adaptor").one()
        assert kds.name == "KDS PSU" and kds.default_price_inr == 900
        assert db.query(SpareCatalog).filter_by(product_category="Kitchen Display Screen",
                                                name="Power adaptor").count() == 0
        assert seed_spare_catalog(db) == 0  # a second restart changes nothing
    names = [n for _, n, _ in DEFAULT_CATALOG]
    assert not any("adapter" in n.lower() for n in names)
