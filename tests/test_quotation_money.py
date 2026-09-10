from decimal import Decimal

import pytest

from app.services.quotation_money import (
    compute_totals,
    fmt_inr,
    fmt_qty,
    fmt_rate,
    line_total,
    q2,
)


@pytest.mark.parametrize(
    "value, expected",
    [
        (0, "0.00"),
        (5, "5.00"),
        (870, "870.00"),
        (999, "999.00"),
        (1000, "1,000.00"),
        (38000, "38,000.00"),
        (49470, "49,470.00"),
        ("8904.60", "8,904.60"),
        ("58374.6", "58,374.60"),
        (110940, "1,10,940.00"),
        ("19969.2", "19,969.20"),
        ("130909.2", "1,30,909.20"),
        (204000, "2,04,000.00"),
        (1234567, "12,34,567.00"),
        (12345678, "1,23,45,678.00"),
        (123456789, "12,34,56,789.00"),
        ("-1234.5", "-1,234.50"),
        (0.1 + 0.2, "0.30"),  # float goes via str() — no 0.30000000000000004
    ],
)
def test_fmt_inr(value, expected):
    assert fmt_inr(value) == expected


def test_q2_rounds_half_up_not_bankers():
    assert q2("0.125") == Decimal("0.13")
    assert q2("0.135") == Decimal("0.14")
    assert q2("2.675") == Decimal("2.68")
    assert q2(Decimal("1.005")) == Decimal("1.01")


def test_line_total_and_totals_navapakam():
    items = [(38000, 1), (6800, 1), (3800, 1), (870, 1)]
    t = compute_totals(items, 18)
    assert t.subtotal == Decimal("49470.00")
    assert t.gst_amount == Decimal("8904.60")
    assert t.grand_total == Decimal("58374.60")
    assert fmt_inr(t.grand_total) == "58,374.60"


def test_totals_happy_table_cctv():
    items = [
        (1750, 10), (1850, 3), (8800, 1), (1700, 1), (17500, 1), (1800, 1),
        (50, 13), (50, 26), (40, 16), (50, 360), (50, 50), (75, 200),
        (7000, 1), (4000, 1), (9000, 1),
    ]
    t = compute_totals(items, 18)
    assert fmt_inr(t.subtotal) == "1,10,940.00"
    assert fmt_inr(t.gst_amount) == "19,969.20"
    assert fmt_inr(t.grand_total) == "1,30,909.20"


def test_totals_penny_lane():
    t = compute_totals([(45000, 2), (7500, 6), (13800, 5)], 18)
    assert fmt_inr(t.subtotal) == "2,04,000.00"
    assert fmt_inr(t.gst_amount) == "36,720.00"
    assert fmt_inr(t.grand_total) == "2,40,720.00"


def test_line_totals_are_rounded_per_line_before_summing():
    # 3 × 33.335 = 100.005 -> each line rounds to 100.01 (half-up on 100.005)
    assert line_total("33.335", 3) == Decimal("100.01")
    t = compute_totals([("33.335", 3)], 18)
    assert t.subtotal == Decimal("100.01")
    assert t.gst_amount == Decimal("18.00")  # 18.0018 -> 18.00


def test_fractional_quantity_and_zero_gst():
    t = compute_totals([("100", "1.5")], 0)
    assert t.subtotal == Decimal("150.00")
    assert t.gst_amount == Decimal("0.00")
    assert t.grand_total == Decimal("150.00")


def test_fmt_rate_and_qty():
    assert fmt_rate(18) == "18"
    assert fmt_rate("18.00") == "18"
    assert fmt_rate("12.5") == "12.5"
    assert fmt_qty(360) == "360"
    assert fmt_qty("2.00") == "2"
    assert fmt_qty("1.5") == "1.5"
