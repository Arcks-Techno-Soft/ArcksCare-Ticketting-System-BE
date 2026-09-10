"""Money arithmetic + Indian-format display for quotations.

Decimal end to end, ROUND_HALF_UP to 2 dp (plan §5.4):

    line_total  = unit_price × quantity
    subtotal    = Σ line_total
    gst_amount  = subtotal × gst_rate / 100
    grand_total = subtotal + gst_amount

`fmt_inr` prints with Indian digit grouping (3-then-2): 1,10,940.00.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Iterable, List, Sequence, Tuple, Union

Number = Union[Decimal, int, float, str]

TWO_PLACES = Decimal("0.01")


def D(value: Number) -> Decimal:
    """Coerce to Decimal without float artefacts (floats go via str)."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Not a number: {value!r}") from exc


def q2(value: Number) -> Decimal:
    """Round to 2 dp, half-up (never banker's rounding on a price document)."""
    return D(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def line_total(unit_price: Number, quantity: Number) -> Decimal:
    return q2(D(unit_price) * D(quantity))


@dataclass(frozen=True)
class Totals:
    line_totals: Tuple[Decimal, ...]
    subtotal: Decimal
    gst_rate: Decimal
    gst_amount: Decimal
    grand_total: Decimal


def compute_totals(
    items: Iterable[Tuple[Number, Number]], gst_rate: Number
) -> Totals:
    """`items` = iterable of (unit_price, quantity)."""
    lines: List[Decimal] = [line_total(p, q) for p, q in items]
    subtotal = q2(sum(lines, Decimal("0")))
    rate = D(gst_rate)
    gst_amount = q2(subtotal * rate / Decimal(100))
    grand_total = q2(subtotal + gst_amount)
    return Totals(
        line_totals=tuple(lines),
        subtotal=subtotal,
        gst_rate=rate,
        gst_amount=gst_amount,
        grand_total=grand_total,
    )


def fmt_inr(value: Number) -> str:
    """Indian grouping, always 2 dp, no currency sign: 1,10,940.00.

    Negative values keep the sign in front (-1,234.50) — not expected on a
    quotation but the formatter shouldn't produce garbage if it happens.
    """
    amount = q2(value)
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    whole, _, frac = f"{amount:.2f}".partition(".")
    if len(whole) <= 3:
        grouped = whole
    else:
        head, last3 = whole[:-3], whole[-3:]
        pairs = []
        while len(head) > 2:
            pairs.insert(0, head[-2:])
            head = head[:-2]
        pairs.insert(0, head)
        grouped = ",".join(pairs) + "," + last3
    return f"{sign}{grouped}.{frac}"


def fmt_rate(rate: Number) -> str:
    """GST rate for the label: 18 -> '18', 12.5 -> '12.5'."""
    r = D(rate).normalize()
    if r == r.to_integral():
        return str(int(r))
    return format(r, "f")


def fmt_qty(qty: Number) -> str:
    """Quantity for print: whole numbers without decimals, else up to 2 dp."""
    q = D(qty).normalize()
    if q == q.to_integral():
        return str(int(q))
    return format(q2(q).normalize(), "f")
