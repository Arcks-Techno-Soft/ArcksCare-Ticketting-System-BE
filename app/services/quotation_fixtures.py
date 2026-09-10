"""Golden fixtures (plan §12) — plain dicts so they double as API examples.

`NAVAPAKAM_DRAFT` recreates quotation-assets/samples/sample-pos-navapakam-kitchens.pdf
(POS variant: detailed rows, images, BASIC labels, green-on-black note).
`HAPPY_TABLE_DRAFT` recreates sample-cctv-happy-table-ventures.pdf
(CCTV variant: compact rows with Sl No., SIMPLE labels, red note).
"""
from __future__ import annotations

M95_SPEC = "\n".join([
    "BUILT IN C P U , [[Intel® N95]]",
    "[[12th Generation Processor]]",
    "[[STORAGE MEMORY : 128GB NVMe SSD]]",
    "[[(Fast Response)]]",
    "[[RAM : 8 GB DDR4L (Low Voltage)]]",
    "[[Upgradable upto 32GB]]",
    "INTERFACE PORTS",
    "Serial : 6 Nos , USB PORTS : 6 No. LAN : 1 No.",
    "VGA (Indipendent) : 1 No , AUDIO : 1 Line in 1 Line Out",
    "TOUCH MONITOR",
    "True Flat PCAP - Projection CapacitiveTouch Screen Spilage Proof and Dust Proof",
    'DISPLAY SIZE : 15.1" PCAP Touch Screen',
    "TOUCH INTERFACE : USB",
    "[[With Built in Speakers]]",
])

S200E_SPEC = "\n".join([
    "INTERFACE PORTS",
    "USB+LAN+Cash Drawer",
    "[[With Auto cutter & KOT Alaram]]",
    "Print Resolution: 203.2 dpi (8dots/mm)",
    "Sensors: Paper End, Cover Open, Print Alarm",
    "Power Supply: 24V, 2.5A",
    "Colour : Dark Grey",
])

SKC582_SPEC = "\n".join([
    "Body: Metal with heavy-duty ball bearing rollers",
    "Slots: 5 Bill, 8 Coin",
    "Interface: RJ-11",
    "Removable Bill & Coin Dividers",
    "Manual Lock & Key Operation",
    "Colour : Dark Grey",
])

NAVAPAKAM_DRAFT = {
    "quotation_date": "2026-07-14",
    "reference": "14072920/2026-27",
    "customer_name": "NAVAPAKAM KITCHENS LLP",
    "address_lines": [
        "No.5, 3rd Main, Rama Mandira Road",
        "Kamakshipalya, Magadi Road",
        "Kaveripura, Bengaluru - 560079",
    ],
    "customer_gstin": "29AAUFN8185B1ZN",
    "customer_pan": None,
    "subject_line": "For Sarjapur Road outlet",
    "signatory_id": 1,
    "validity_days": 15,
    "gst_rate": 18,
    "totals_label_set": "BASIC",
    "note_text": "Note : 24/7 x 365 Onsite Service Support",
    "note_style": "GREEN_ON_BLACK",
    "terms_preset": "POS",
    "items": [
        {
            "row_style": "DETAILED",
            "brand": "SK-POS®",
            "model": "Mighty Series\nM95",
            "headline": (
                "Touch POS System with [[N95 Processor]] - [[8GB RAM]] with "
                "[[128 GB M.2 NVMe SSD (Fastest Response)]] Capacitive Flat Touch "
                "Screen with Water Proof and Dust Proof"
            ),
            "spec_lines": M95_SPEC,
            "warranty_label": "3 Years Onsite Warranty",
            "unit_price": 38000,
            "quantity": 1,
            "include_image": True,
            "image_asset": "sk-pos-m95-touch-pos.png",
        },
        {
            "row_style": "DETAILED",
            "brand": "STOUT®",
            "brand_sub_label": "by SK-POS®",
            "model": "S200E",
            "headline": "High Speed Thermal Printer with multiple interface for Bill Printing/KOT",
            "spec_lines": S200E_SPEC,
            "warranty_label": "1 Year Onsite Warranty",
            "unit_price": 6800,
            "quantity": 1,
            "include_image": True,
            "image_asset": "stout-s200e-thermal-printer.png",
        },
        {
            "row_style": "DETAILED",
            "brand": "SK-POS®",
            "model": "SKC582",
            "headline": "Auto Openable Cash Drawer",
            "spec_lines": SKC582_SPEC,
            "warranty_label": "1 Year Onsite Warranty",
            "unit_price": 3800,
            "quantity": 1,
            "include_image": True,
            "image_asset": "sk-pos-skc582-cash-drawer.png",
        },
        {
            "row_style": "COMPACT",
            "brand": "DELL",
            "model": None,
            "headline": "Wired Keybaord and Mouse",
            "unit_price": 870,
            "quantity": 1,
            "include_image": False,
        },
    ],
}


def _compact(brand, headline, price, qty):
    return {
        "row_style": "COMPACT",
        "brand": brand,
        "model": None,
        "headline": headline,
        "unit_price": price,
        "quantity": qty,
        "include_image": False,
    }


HAPPY_TABLE_DRAFT = {
    "quotation_date": "2026-09-10",
    "reference": "0108SW048/2026-27",
    "customer_name": "Happy table ventures pvt Ltd",
    "address_lines": ["Banashankari", "Bangalore"],
    "subject_line": None,
    "signatory_id": 1,
    "validity_days": 15,
    "gst_rate": 18,
    "totals_label_set": "SIMPLE",
    "note_text": "Note: Cabling and Monitor not added in the above Prices",
    "note_style": "RED_TEXT",
    "terms_preset": "CCTV",
    "items": [
        _compact("CP PLUS", "HD DOME 2 MP CAMERA WITH MIC", 1750, 10),
        _compact("CP PLUS", "HD  2 MP CAMERA BULLET WHITH MIC", 1850, 3),
        _compact("CP PLUS", "16 CHANNEL HD DVR", 8800, 1),
        _compact("CP PLUS", "16 CHANNEL SMPS POWER SUPPLY", 1700, 1),
        _compact(None, "4TB SURVEILLANCE HARD DISK FOR SECURITY CAMERAS", 17500, 1),
        _compact(None, "NETWORK SERVER RACK 4U", 1800, 1),
        _compact(None, "DC PIN", 50, 13),
        _compact(None, "BNC PIN", 50, 26),
        _compact(None, "PVC BOX", 40, 16),
        _compact(None, "CCTV 1+3 CABLE 360 mtr", 50, 360),
        _compact(None, "PVC PIPE MATIRIAL 50 LENGTHS", 50, 50),
        _compact(None, "CAT 6 NETWORK CABLE", 75, 200),
        _compact(None, "TP-LINK ACCESS POINT", 7000, 1),
        _compact(None, "TP-LINK 8 PORT NETWORK SWITCH", 4000, 1),
        _compact(
            None,
            "Cameras Fixing, Alignment ,DVR Installation Rack Fixing & Mobile "
            "Configuration , Mobile App Installation & Testing Charges",
            9000, 1,
        ),
    ],
}

FIXTURES = {"navapakam": NAVAPAKAM_DRAFT, "happy-table": HAPPY_TABLE_DRAFT}
