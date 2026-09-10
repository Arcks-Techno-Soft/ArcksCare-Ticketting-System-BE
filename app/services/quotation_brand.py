"""Fixed brand content + asset paths + font registration for quotations.

Everything printed on a quotation that is NOT typed by the admin lives here:
company identity, bank details, the signatory list, the T&C presets, the
totals-label sets, and the paths of the bundled images / fonts.

Fonts are open look-alikes ("Option B" in QUOTATIONS_FEATURE_PLAN.md §11) so
the renderer works on any box — the Lightsail VM has no Microsoft fonts. The
mapping below is the single place to swap in the real Microsoft TTFs later
(Option A): drop the files into `assets/quotation/fonts/` and change FONT_FILES.

    logical name   Excel template font        open substitute
    Q-Title        Arial Black                Archivo Black
    Q-Sans         Calibri Bold               Carlito Bold (metric-compatible)
    Q-SansRegular  Calibri                    Carlito Regular
    Q-Serif        Lucida Fax Demibold        Bitter (slab serif; no metric clone)
    Q-Rupee        Times New Roman Bold (₹)   Ubuntu Bold (Liberation Serif Bold
                                              — the TNR clone — has no ₹ glyph)
    Q-Times        Times New Roman Bold       Liberation Serif Bold
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

logger = logging.getLogger("skposcare.quotation")

# ----------------------------- asset paths ------------------------------- #

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets" / "quotation"
FONTS_DIR = ASSETS_DIR / "fonts"
PRODUCT_IMAGES_DIR = ASSETS_DIR / "products"

LOGO_BANNER = ASSETS_DIR / "logo-banner.png"          # 1801 × 542, black banner
QR_SCAN_PAY = ASSETS_DIR / "qr-scan-pay.png"          # 442 × 511, incl. caption
STAMP_SIGNATURE = ASSETS_DIR / "stamp-signature.png"  # 1000 × 924 RGBA


def bundled_product_images() -> Dict[str, Path]:
    """Seed product photos shipped with the app, keyed by file name.

    Used by the preview endpoint's `image_asset` field (and later by the
    catalogue seed). Only files that actually exist in the folder are
    accepted, so a client can never point outside the assets directory.
    """
    if not PRODUCT_IMAGES_DIR.is_dir():
        return {}
    return {
        p.name: p
        for p in sorted(PRODUCT_IMAGES_DIR.iterdir())
        if p.suffix.lower() in {".png", ".jpg", ".jpeg"} and p.is_file()
    }


# ----------------------------- fonts ------------------------------------- #

FONT_FILES: Dict[str, str] = {
    "Q-Title": "ArchivoBlack-Regular.ttf",
    "Q-Sans": "Carlito-Bold.ttf",
    "Q-SansRegular": "Carlito-Regular.ttf",
    "Q-Serif": "Bitter-Bold.ttf",
    "Q-SerifSemi": "Bitter-SemiBold.ttf",
    "Q-Rupee": "Ubuntu-Bold.ttf",
    "Q-Times": "LiberationSerif-Bold.ttf",
}

_fonts_lock = threading.Lock()
_fonts_registered = False


def register_fonts() -> None:
    """Register the quotation fonts with ReportLab (idempotent, thread-safe).

    Raises FileNotFoundError if a font file is missing — better to fail loudly
    at first render than to silently fall back to Helvetica and ship a
    quotation that looks nothing like the template.
    """
    global _fonts_registered
    if _fonts_registered:
        return
    with _fonts_lock:
        if _fonts_registered:
            return
        for logical, filename in FONT_FILES.items():
            path = FONTS_DIR / filename
            if not path.is_file():
                raise FileNotFoundError(
                    f"Quotation font {logical} missing: {path}. "
                    "See QUOTATIONS_FEATURE_PLAN.md §11."
                )
            pdfmetrics.registerFont(TTFont(logical, str(path)))
        _fonts_registered = True
        logger.info("Quotation fonts registered from %s", FONTS_DIR)


# ----------------------------- company ----------------------------------- #

COMPANY_NAME = "S K TECHNOSYS PRIVATE LIMITED"
COMPANY_GSTIN = "29ABNCS9284M1Z4"
COMPANY_WHATSAPP = "8105625375"
COMPANY_WEBSITE = "www.sktechnosys.in"
COMPANY_TAGLINE = "Empower Your Business with our Solutions Brings out the Best"
COMPANY_OFFICE_LINE = (
    "OFFICE : # 259,  Second Floor, Above SBI , Opp.BMTC Bus Stop, ISRO Layout,  "
    "Bengaluru – 560078 Karnataka"
)

# (label, value) in print order — the bank block prints exactly these rows.
BANK_DETAILS: List[tuple] = [
    ("Company Name", COMPANY_NAME),
    ("Bank Name", "HDFC BANK"),
    ("Branch", "ISRO LAYOUT"),
    ("Account No", "50200119100481"),
    ("IFSC Code", "HDFC0008446"),
    ("Account Type", "Current Account"),
]

SIGNATURE_BAND_TEXT = f"FOR {COMPANY_NAME}"
BANK_BAND_TEXT = "BANK DETAILS"


# ----------------------------- signatories ------------------------------- #

@dataclass(frozen=True)
class Signatory:
    id: int
    name: str            # printed under the stamp, upper case
    designation: str     # printed in brackets under the name
    phones: str          # printed as-is under the designation
    email: str           # header "Email" line
    mobile: str          # header "Mobile" line (first phone)
    initials: str        # for the auto reference (§5.3)


SIGNATORIES: List[Signatory] = [
    Signatory(
        id=1,
        name="SRINIVAS NARAYAN",
        designation="(GM- Operations & Business Development)",
        phones="9343368482 / 8892438828",
        email="nsrini@sktechnosys.in",
        mobile="9343368482",
        initials="SW",
    ),
]
DEFAULT_SIGNATORY_ID = SIGNATORIES[0].id


def get_signatory(signatory_id: Optional[int]) -> Optional[Signatory]:
    for s in SIGNATORIES:
        if s.id == signatory_id:
            return s
    return None


# ----------------------------- totals labels ----------------------------- #

# label set -> (subtotal label, gst label template, grand total label)
TOTALS_LABELS: Dict[str, tuple] = {
    "BASIC": ("TOTAL BASIC PRICE", "GST @ {rate}%", "TOTAL AMOUNT"),
    "SIMPLE": ("TOTAL", "GST @ {rate}%", "GRAND TOTAL"),
}


# ----------------------------- notes ------------------------------------- #

NOTE_PRESETS: Dict[str, tuple] = {
    # style, text
    "POS": ("GREEN_ON_BLACK", "Note : 24/7 x 365 Onsite Service Support"),
    "CCTV": ("RED_TEXT", "Note: Cabling and Monitor not added in the above Prices"),
}


# ----------------------------- terms & conditions ------------------------ #

# Verbatim from the samples (typos kept on purpose so the output matches the
# documents customers already receive — see plan §12/§13 Q9). Line 1 is a
# template: {days} is filled from `validity_days`. By convention line 2 (the
# taxes line) prints red.
TERMS_PRESETS: Dict[str, List[str]] = {
    "POS": [
        "Quotation validity for {days} Days",
        "Taxes : GST @ 18% OR AS APPLICABLE AT THE TIME OF INVOICE",
        "Delivery : Within1 Week  from the Date of Purchase Order",
        "Transportation Charges Extra as applicable",
        "Payment Terms : 100% advance along with P.O through RTGS/NEFT/IMPS/UPI Transfer",
        "Warranty : 3 Years for Touch POS Systems and 1 Year for other items from the "
        "date of Invoice/Supply whichever is earlier. Warranty Covers only manufacturing "
        "defects and does not cover Adaptors, Cables, Wear and tear parts, "
        "breakage,imporper use,abuse and mishandling etc.,  Any Parts burnt & Products "
        "not used with UPS will not cover under Warranty",
    ],
    "CCTV": [
        "Quotation validity for {days} Days",
        "Taxes : 18% GST as mentioned above",
        "Delivery : Within1 Week  from the Date of Purchase Order",
        "Fright : Included",
        "Payment Terms : 100% advance along with P.O through RTGS/NEFT/IMPS/UPI Transfer",
        "Warranty :1 year from the date of Invoice/Supply whichever is earlier. Warranty "
        "Covers only manufacturing defects and does not cover Adaptors, Cables, Wear and "
        "tear parts, breakage,imporper use,abuse and mishandling etc.,",
    ],
}
RED_TERM_INDEX = 1  # zero-based: the taxes line


def terms_for(preset: str, validity_days: int) -> List[str]:
    lines = TERMS_PRESETS[preset]
    return [line.format(days=validity_days) for line in lines]
