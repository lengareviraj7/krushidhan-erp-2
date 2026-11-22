"""
Professional GST Tax Invoice Generator (English-Only A4 Commercial Agri-Retail Layout).
Uses clean English typography (Helvetica / Helvetica-Bold), locally bundled offline
Indian Rupee (₹) glyph rendering, dynamic shop branding, statutory agri-input licences,
full GST breakdown, and XML-safe rendering without any external shaping dependencies.
"""
from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union
from xml.sax.saxutils import escape as xml_escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from src.config import resolve_bundled_resource
from src.models.sales import Sale
from src.models.system import CompanySettings

logger = logging.getLogger("agri_erp.printing")

# Register bundled TrueType font solely for offline Indian Rupee symbol (₹ / U+20B9)
# because standard Type1 WinAnsi (Helvetica) and ReportLab Vera lack the ₹ glyph.
FONTS_DIR = resolve_bundled_resource("src", "printing", "fonts")
REG_RUPEE_FONT_PATH = FONTS_DIR / "NotoSansDevanagari-Regular.ttf"
BOLD_RUPEE_FONT_PATH = FONTS_DIR / "NotoSansDevanagari-Bold.ttf"

HAS_RUPEE_FONT = False
if REG_RUPEE_FONT_PATH.exists() and BOLD_RUPEE_FONT_PATH.exists():
    try:
        pdfmetrics.registerFont(TTFont("RupeeRegular", str(REG_RUPEE_FONT_PATH)))
        pdfmetrics.registerFont(TTFont("RupeeBold", str(BOLD_RUPEE_FONT_PATH)))
        pdfmetrics.registerFontFamily("Rupee", normal="RupeeRegular", bold="RupeeBold")
        HAS_RUPEE_FONT = True
    except Exception as e:
        logger.warning("Could not register bundled Rupee font: %s", e)

FONT_ENGLISH = "Helvetica"
FONT_ENGLISH_BOLD = "Helvetica-Bold"
FONT_RUPEE = "RupeeRegular" if HAS_RUPEE_FONT else FONT_ENGLISH
FONT_RUPEE_BOLD = "RupeeBold" if HAS_RUPEE_FONT else FONT_ENGLISH_BOLD

# Backwards-compatible aliases if referenced externally
FONT_NAME = FONT_ENGLISH
FONT_BOLD = FONT_ENGLISH_BOLD

_XML_ENTITIES = {'"': "&quot;", "'": "&apos;"}


def _sanitize_english_text(text: Any) -> str:
    """
    Normalize English punctuation and keep printable WinAnsi/Latin-1 characters
    representable cleanly in Helvetica plus the Indian Rupee symbol (₹).
    Drops unsupported scripts/control characters cleanly so PDFs never render blank boxes.
    """
    raw = "" if text is None else str(text)
    if not raw:
        return ""

    out: List[str] = []
    for ch in raw:
        if ch == "\u20b9":
            out.append(ch)
            continue
        if ch in ("\u2013", "\u2014", "\u2022"):
            out.append("-")
            continue
        if ch in ("\u2018", "\u2019"):
            out.append("'")
            continue
        if ch in ("\u201c", "\u201d"):
            out.append('"')
            continue
        if ch == "\u00a0":
            out.append(" ")
            continue
        code = ord(ch)
        if ch in ("\n", "\r", "\t") or (0x20 <= code <= 0x7E) or (0xA0 <= code <= 0xFF):
            out.append(ch)
    return "".join(out)


def _english_markup(text: Any, *, bold: bool = False) -> str:
    """
    Convert English text into ReportLab Paragraph XML markup:
    - Escapes XML characters (&, <, >, ", ').
    - Renders ₹ using the bundled offline Rupee font (or 'Rs.' fallback).
    - Renders all English text cleanly in Helvetica / Helvetica-Bold.
    """
    clean = _sanitize_english_text(text)
    if not clean:
        return ""

    if not HAS_RUPEE_FONT:
        escaped = xml_escape(clean.replace("₹", "Rs."), _XML_ENTITIES)
        return f"<b>{escaped}</b>" if bold and escaped else escaped

    if "₹" not in clean:
        escaped = xml_escape(clean, _XML_ENTITIES)
        return f"<b>{escaped}</b>" if bold and escaped else escaped

    rupee_font = FONT_RUPEE_BOLD if bold else FONT_RUPEE
    parts: List[str] = []
    segments = clean.split("₹")
    for idx, seg in enumerate(segments):
        if seg:
            esc = xml_escape(seg, _XML_ENTITIES)
            if esc:
                parts.append(f"<b>{esc}</b>" if bold else esc)
        if idx < len(segments) - 1:
            parts.append(f'<font name="{rupee_font}">₹</font>')
    return "".join(parts)


def _format_currency(amount: float, *, bold: bool = False, show_symbol: bool = True) -> str:
    """Format INR currency string cleanly for ReportLab paragraphs."""
    val = float(amount or 0.0)
    sign = "-" if val < -0.004 else ""
    abs_val = abs(val)
    prefix = f"{sign}₹ " if show_symbol else sign
    return _english_markup(f"{prefix}{abs_val:,.2f}", bold=bold)


def _format_payment_mode(mode: Optional[str]) -> str:
    if not mode:
        return "CASH"
    normalized = str(mode).strip().upper()
    mapping = {
        "CASH": "CASH",
        "CREDIT": "CREDIT",
        "BANK_TRANSFER": "BANK TRANSFER",
        "CHEQUE": "CHEQUE",
        "CARD": "CARD",
        "SPLIT": "SPLIT",
        "UPI": "UPI (Historical)",
    }
    return mapping.get(normalized, normalized.replace("_", " "))


def _compute_payment_status(net_amount: float, paid_amount: float, due_amount: float) -> Tuple[str, str]:
    """Return (status_label, hex_color) from invoice amounts."""
    effective_due = due_amount if due_amount > 0.005 else max(0.0, round(net_amount - paid_amount, 2))
    if effective_due <= 0.01:
        return ("PAID", "#15803d")
    if paid_amount > 0.01:
        return ("PARTIAL", "#b45309")
    return ("CREDIT / UNPAID", "#b91c1c")


class InvoicePrinter:
    """Generates dynamic, shop-branded GST Tax Invoices (A4 PDF)."""

    def __init__(self, company_settings: Optional[CompanySettings] = None):
        self.settings = company_settings or CompanySettings(
            company_name="Agri-Input Shop",
            state="",
        )

    def _number_to_words(self, number: float) -> str:
        """Convert Indian Rupee amount (including paise if non-zero) to words."""
        units = ["", "ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX", "SEVEN", "EIGHT", "NINE"]
        teens = [
            "TEN",
            "ELEVEN",
            "TWELVE",
            "THIRTEEN",
            "FOURTEEN",
            "FIFTEEN",
            "SIXTEEN",
            "SEVENTEEN",
            "EIGHTEEN",
            "NINETEEN",
        ]
        tens = ["", "", "TWENTY", "THIRTY", "FORTY", "FIFTY", "SIXTY", "SEVENTY", "EIGHTY", "NINETY"]

        def convert_less_than_thousand(n: int) -> str:
            res = ""
            if n >= 100:
                res += units[n // 100] + " HUNDRED "
                n %= 100
            if n >= 20:
                res += tens[n // 10] + " "
                n %= 10
            elif n >= 10:
                res += teens[n - 10] + " "
                n = 0
            if n > 0:
                res += units[n] + " "
            return res.strip()

        def integer_words(val: int) -> str:
            if val <= 0:
                return ""
            crore = val // 10000000
            rem = val % 10000000
            lakh = rem // 100000
            rem %= 100000
            thousand = rem // 1000
            hundreds = rem % 1000
            parts = []
            if crore > 0:
                parts.append(f"{integer_words(crore)} CRORE")
            if lakh > 0:
                parts.append(f"{convert_less_than_thousand(lakh)} LAKH")
            if thousand > 0:
                parts.append(f"{convert_less_than_thousand(thousand)} THOUSAND")
            if hundreds > 0:
                parts.append(convert_less_than_thousand(hundreds))
            return " ".join(parts)

        rounded_paise = int(round(max(0.0, float(number or 0.0)) * 100))
        rupees_int = rounded_paise // 100
        paise_int = rounded_paise % 100

        if rupees_int == 0 and paise_int == 0:
            return "ZERO RUPEES ONLY"

        rupee_str = f"RUPEES {integer_words(rupees_int)}" if rupees_int > 0 else "RUPEES ZERO"
        if paise_int > 0:
            return f"{rupee_str} AND {convert_less_than_thousand(paise_int)} PAISE ONLY"
        return f"{rupee_str} ONLY"

    def _build_shop_address(self) -> str:
        """Assemble clean postal address only from configured shop fields."""
        addr = (self.settings.address or "").strip()
        city = (self.settings.city or "").strip()
        state = (self.settings.state or "").strip()
        pincode = (self.settings.pincode or "").strip()

        has_specific_location = any([addr, city, pincode])
        parts: List[str] = []
        if addr:
            parts.append(addr)
        if city and city.lower() not in addr.lower():
            parts.append(city)
        if has_specific_location and state and state.lower() not in " ".join(parts).lower():
            if pincode and pincode not in state:
                parts.append(f"{state} - {pincode}")
            else:
                parts.append(state)
        elif pincode and pincode not in " ".join(parts):
            parts.append(f"PIN: {pincode}")
        return ", ".join(parts)

    @staticmethod
    def _build_customer_address(sale: Sale) -> str:
        raw_parts = [
            (sale.customer_village or "").strip(),
            (sale.customer_taluka or "").strip(),
            (sale.customer_district or "").strip(),
        ]
        deduped: List[str] = []
        seen = set()
        for part in raw_parts:
            if part and part.lower() not in seen:
                deduped.append(part)
                seen.add(part.lower())
        return ", ".join(deduped)

    def generate_invoice_pdf(
        self,
        sale: Sale,
        output_path: Optional[Union[str, Path]] = None,
        page_size=A4,
    ) -> bytes:
        """Generate a clean commercial A4 GST Tax Invoice PDF using authenticated shop & sale data."""
        buffer = io.BytesIO()
        target_file = str(output_path) if output_path else buffer

        # 8mm margins -> 194mm usable printable width on A4 (210mm)
        doc = SimpleDocTemplate(
            target_file,
            pagesize=page_size,
            leftMargin=8 * mm,
            rightMargin=8 * mm,
            topMargin=8 * mm,
            bottomMargin=8 * mm,
        )

        story = []
        styles = getSampleStyleSheet()

        brand_accent = colors.HexColor("#14532d")
        border_dark = colors.HexColor("#334155")
        border_light = colors.HexColor("#cbd5e1")
        header_bg = colors.HexColor("#f1f5f9")
        zebra_bg = colors.HexColor("#f8fafc")
        highlight_bg = colors.HexColor("#ecfdf5")
        text_dark = colors.HexColor("#0f172a")
        text_muted = colors.HexColor("#475569")

        shop_title_style = ParagraphStyle(
            "ShopTitle",
            parent=styles["Normal"],
            fontName=FONT_ENGLISH_BOLD,
            fontSize=15,
            leading=18,
            textColor=brand_accent,
        )
        normal_style = ParagraphStyle(
            "InvNormal",
            parent=styles["Normal"],
            fontName=FONT_ENGLISH,
            fontSize=8.5,
            leading=11.5,
            textColor=text_dark,
        )
        small_style = ParagraphStyle(
            "InvSmall",
            parent=styles["Normal"],
            fontName=FONT_ENGLISH,
            fontSize=8,
            leading=11,
            textColor=text_dark,
        )
        small_right = ParagraphStyle("InvSmallR", parent=small_style, alignment=2)
        small_center = ParagraphStyle("InvSmallC", parent=small_style, alignment=1)
        table_hdr_center = ParagraphStyle(
            "TblHdrC",
            parent=styles["Normal"],
            fontName=FONT_ENGLISH_BOLD,
            fontSize=7,
            leading=9,
            alignment=1,
            textColor=text_dark,
        )
        table_hdr_left = ParagraphStyle("TblHdrL", parent=table_hdr_center, alignment=0)
        table_hdr_right = ParagraphStyle("TblHdrR", parent=table_hdr_center, alignment=2)
        cell_left = ParagraphStyle(
            "TblCellL",
            parent=styles["Normal"],
            fontName=FONT_ENGLISH,
            fontSize=7.2,
            leading=9.2,
            alignment=0,
            textColor=text_dark,
        )
        cell_center = ParagraphStyle("TblCellC", parent=cell_left, alignment=1)
        cell_right = ParagraphStyle("TblCellR", parent=cell_left, alignment=2)
        terms_style = ParagraphStyle(
            "InvTerms",
            parent=styles["Normal"],
            fontName=FONT_ENGLISH,
            fontSize=7.5,
            leading=10,
            textColor=text_muted,
        )
        footer_center = ParagraphStyle(
            "InvFooterC",
            parent=styles["Normal"],
            fontName=FONT_ENGLISH,
            fontSize=7,
            leading=9.5,
            alignment=1,
            textColor=text_muted,
        )

        # -------------------------------------------------------------------------
        # 1. TOP DOCUMENT STRIP (TAX INVOICE + MODE + STATUS)
        # -------------------------------------------------------------------------
        pay_mode_label = _format_payment_mode(sale.payment_mode)
        effective_due = (
            float(sale.due_amount)
            if float(sale.due_amount or 0.0) > 0.005
            else max(0.0, round(float(sale.net_amount or 0.0) - float(sale.paid_amount or 0.0), 2))
        )
        status_label, status_color = _compute_payment_status(
            float(sale.net_amount or 0.0),
            float(sale.paid_amount or 0.0),
            effective_due,
        )

        top_bar_left = Paragraph(
            _english_markup("TAX INVOICE", bold=True),
            ParagraphStyle("DocType", parent=styles["Normal"], fontName=FONT_ENGLISH_BOLD, fontSize=11, leading=13, textColor=brand_accent),
        )
        top_bar_right = Paragraph(
            f"{_english_markup('Mode:', bold=True)} {_english_markup(pay_mode_label, bold=True)}"
            f" &nbsp;|&nbsp; {_english_markup('Status:', bold=True)} "
            f"<font color='{status_color}'>{_english_markup(status_label, bold=True)}</font>",
            ParagraphStyle("DocStatus", parent=small_right, fontSize=8.5, leading=12),
        )
        top_bar = Table([[top_bar_left, top_bar_right]], colWidths=[80 * mm, 114 * mm])
        top_bar.setStyle(
            TableStyle(
                [
                    ("BOX", (0, 0), (-1, -1), 0.9, border_dark),
                    ("BACKGROUND", (0, 0), (-1, -1), header_bg),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        story.append(top_bar)

        # -------------------------------------------------------------------------
        # 2. DYNAMIC SHOP HEADER + STATUTORY REGISTRATION BOX
        # -------------------------------------------------------------------------
        shop_name = (self.settings.company_name or "").strip() or "Agri-Input Shop"
        owner_name = (self.settings.owner_name or "").strip()
        full_shop_address = self._build_shop_address()
        shop_mobile = (self.settings.mobile or "").strip()
        shop_email = (self.settings.email or "").strip()
        shop_gstin = (self.settings.gstin or "").strip()
        lic_fert = (self.settings.dl_fertilizer or "").strip()
        lic_pest = (self.settings.dl_pesticide or "").strip()
        lic_seed = (self.settings.dl_seed or "").strip()

        shop_left_lines: List[str] = [_english_markup(shop_name, bold=True)]
        shop_body_lines: List[str] = []
        if owner_name:
            shop_body_lines.append(
                f"{_english_markup('Proprietor:', bold=True)} {_english_markup(owner_name)}"
            )
        if full_shop_address:
            shop_body_lines.append(_english_markup(full_shop_address))
        contact_parts: List[str] = []
        if shop_mobile:
            contact_parts.append(f"{_english_markup('Mobile:', bold=True)} {_english_markup(shop_mobile)}")
        if shop_email:
            contact_parts.append(f"{_english_markup('Email:', bold=True)} {_english_markup(shop_email)}")
        if contact_parts:
            shop_body_lines.append(" &nbsp;|&nbsp; ".join(contact_parts))

        shop_left_flow = [Paragraph(shop_left_lines[0], shop_title_style)]
        if shop_body_lines:
            shop_left_flow.append(Spacer(1, 2))
            shop_left_flow.append(Paragraph("<br/>".join(shop_body_lines), normal_style))

        statutory_lines: List[str] = []
        if shop_gstin:
            statutory_lines.append(f"{_english_markup('GSTIN:', bold=True)} {_english_markup(shop_gstin, bold=True)}")
        if lic_fert:
            statutory_lines.append(f"{_english_markup('Fertilizer Lic.:', bold=True)} {_english_markup(lic_fert)}")
        if lic_pest:
            statutory_lines.append(f"{_english_markup('Pesticide Lic.:', bold=True)} {_english_markup(lic_pest)}")
        if lic_seed:
            statutory_lines.append(f"{_english_markup('Seed Lic.:', bold=True)} {_english_markup(lic_seed)}")

        if statutory_lines:
            stat_html = (
                f"{_english_markup('STATUTORY & TAX REGISTRATIONS', bold=True)}<br/>"
                + "<br/>".join(statutory_lines)
            )
            header_table = Table(
                [[shop_left_flow, Paragraph(stat_html, small_style)]],
                colWidths=[120 * mm, 74 * mm],
            )
            header_style_cmds = [
                ("BOX", (0, 0), (-1, -1), 0.9, border_dark),
                ("LINEBEFORE", (1, 0), (1, 0), 0.6, border_dark),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]
        else:
            header_table = Table([[shop_left_flow]], colWidths=[194 * mm])
            header_style_cmds = [
                ("BOX", (0, 0), (-1, -1), 0.9, border_dark),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]
        header_table.setStyle(TableStyle(header_style_cmds))
        story.append(header_table)

        # -------------------------------------------------------------------------
        # 3. BUYER / CUSTOMER & INVOICE METADATA BLOCK
        # -------------------------------------------------------------------------
        cust_name = (sale.customer_name or "").strip() or "Cash Customer"
        cust_addr = self._build_customer_address(sale)
        cust_mobile = (sale.customer_mobile or "").strip()
        cust_gstin = (sale.customer_gstin or "").strip()
        crop_ref = (sale.doctor_or_officer or "").strip()

        cust_lines = [
            f"{_english_markup('Customer:', bold=True)} {_english_markup(cust_name, bold=True)}"
        ]
        if cust_addr:
            cust_lines.append(f"{_english_markup('Address:', bold=True)} {_english_markup(cust_addr)}")
        if cust_mobile:
            cust_lines.append(f"{_english_markup('Mobile:', bold=True)} {_english_markup(cust_mobile)}")
        if cust_gstin:
            cust_lines.append(f"{_english_markup('Customer GSTIN:', bold=True)} {_english_markup(cust_gstin, bold=True)}")
        if crop_ref:
            cust_lines.append(f"{_english_markup('Crop / Ref.:', bold=True)} {_english_markup(crop_ref)}")

        inv_lines = [
            f"{_english_markup('Invoice No.:', bold=True)} {_english_markup(sale.invoice_no, bold=True)}",
            f"{_english_markup('Invoice Date:', bold=True)} {_english_markup(sale.sale_date)}",
            f"{_english_markup('Payment Mode:', bold=True)} {_english_markup(pay_mode_label)}",
            f"{_english_markup('Payment Status:', bold=True)} <font color='{status_color}'>{_english_markup(status_label, bold=True)}</font>",
        ]

        meta_table = Table(
            [[Paragraph("<br/>".join(cust_lines), small_style), Paragraph("<br/>".join(inv_lines), small_style)]],
            colWidths=[112 * mm, 82 * mm],
        )
        meta_table.setStyle(
            TableStyle(
                [
                    ("BOX", (0, 0), (-1, -1), 0.9, border_dark),
                    ("LINEBEFORE", (1, 0), (1, 0), 0.6, border_dark),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        story.append(meta_table)
        story.append(Spacer(1, 2.5 * mm))

        # -------------------------------------------------------------------------
        # 4. PRODUCT LINE ITEMS TABLE (13 COLUMNS = 194mm EXACT)
        # -------------------------------------------------------------------------
        headers = [
            Paragraph(_english_markup("#", bold=True), table_hdr_center),
            Paragraph(_english_markup("Product Description", bold=True), table_hdr_left),
            Paragraph(_english_markup("HSN", bold=True), table_hdr_center),
            Paragraph(_english_markup("Batch No", bold=True), table_hdr_left),
            Paragraph(_english_markup("Exp Date", bold=True), table_hdr_center),
            Paragraph(_english_markup("Qty", bold=True), table_hdr_right),
            Paragraph(_english_markup("Unit", bold=True), table_hdr_center),
            Paragraph(_english_markup("Rate", bold=True), table_hdr_right),
            Paragraph(_english_markup("Disc", bold=True), table_hdr_right),
            Paragraph(_english_markup("Taxable", bold=True), table_hdr_right),
            Paragraph(_english_markup("GST%", bold=True), table_hdr_right),
            Paragraph(_english_markup("Tax Amt", bold=True), table_hdr_right),
            Paragraph(_english_markup("Total", bold=True), table_hdr_right),
        ]

        item_rows = [headers]
        for idx, item in enumerate(sale.items, start=1):
            gst_pct = float(item.cgst_rate or 0.0) + float(item.sgst_rate or 0.0) + float(item.igst_rate or 0.0)
            tax_amt = float(item.cgst_amount or 0.0) + float(item.sgst_amount or 0.0) + float(item.igst_amount or 0.0)
            disc_amt = float(item.discount_amount or 0.0)

            prod_name = (item.product_name or "").strip() or "-"
            hsn = (item.hsn_code or "").strip() or "-"
            batch_no = (item.batch_no or "").strip() or "-"
            exp_date = (item.exp_date or "").strip() or "-"
            unit = (item.unit_name or "").strip() or "NOS"

            item_rows.append(
                [
                    Paragraph(_english_markup(str(idx)), cell_center),
                    Paragraph(_english_markup(prod_name, bold=True), cell_left),
                    Paragraph(_english_markup(hsn), cell_center),
                    Paragraph(_english_markup(batch_no), cell_left),
                    Paragraph(_english_markup(exp_date), cell_center),
                    Paragraph(_english_markup(f"{float(item.qty):g}"), cell_right),
                    Paragraph(_english_markup(unit), cell_center),
                    Paragraph(_english_markup(f"{float(item.sale_rate):.2f}"), cell_right),
                    Paragraph(_english_markup(f"{disc_amt:.2f}"), cell_right),
                    Paragraph(_english_markup(f"{float(item.taxable_amount):.2f}"), cell_right),
                    Paragraph(_english_markup(f"{gst_pct:g}%"), cell_right),
                    Paragraph(_english_markup(f"{tax_amt:.2f}"), cell_right),
                    Paragraph(_english_markup(f"{float(item.total_amount):.2f}", bold=True), cell_right),
                ]
            )

        # Total width = 194.0mm
        col_widths = [
            6.5 * mm,   # #
            39.5 * mm,  # Product Description
            14.5 * mm,  # HSN
            16.0 * mm,  # Batch No
            16.0 * mm,  # Exp Date
            9.5 * mm,   # Qty
            10.0 * mm,  # Unit
            14.0 * mm,  # Rate
            11.5 * mm,  # Disc
            16.0 * mm,  # Taxable
            10.0 * mm,  # GST%
            14.0 * mm,  # Tax Amt
            16.5 * mm,  # Total
        ]

        items_table_cmds = [
            ("BOX", (0, 0), (-1, -1), 0.9, border_dark),
            ("INNERGRID", (0, 0), (-1, -1), 0.45, border_light),
            ("LINEBELOW", (0, 0), (-1, 0), 0.8, border_dark),
            ("BACKGROUND", (0, 0), (-1, 0), header_bg),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
            ("LEFTPADDING", (0, 0), (-1, -1), 2.0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2.0),
        ]
        for row_idx in range(1, len(item_rows)):
            if row_idx % 2 == 0:
                items_table_cmds.append(("BACKGROUND", (0, row_idx), (-1, row_idx), zebra_bg))

        items_table = Table(item_rows, colWidths=col_widths, repeatRows=1)
        items_table.setStyle(TableStyle(items_table_cmds))
        story.append(items_table)
        story.append(Spacer(1, 2.5 * mm))

        # -------------------------------------------------------------------------
        # 5. TAX BREAKDOWN, TOTALS, AMOUNT IN WORDS & PAYMENT TERMS
        # -------------------------------------------------------------------------
        total_disc = float(sale.total_discount or 0.0)
        total_taxable = float(sale.total_taxable or 0.0)
        total_cgst = float(sale.total_cgst or 0.0)
        total_sgst = float(sale.total_sgst or 0.0)
        total_igst = float(sale.total_igst or 0.0)
        extra_charges = float(sale.extra_charges or 0.0)
        round_off = float(sale.round_off or 0.0)
        net_amount = float(sale.net_amount or 0.0)
        paid_amount = float(sale.paid_amount or 0.0)

        items_line_sum = sum(float(i.total_amount or 0.0) for i in sale.items)
        gross_subtotal = (
            round(items_line_sum + total_disc, 2)
            if sale.items
            else round(total_taxable + total_cgst + total_sgst + total_igst + total_disc, 2)
        )

        # Totals table (78mm total -> [42mm, 36mm])
        totals_rows = [
            [
                Paragraph(_english_markup("Subtotal (Gross)"), small_style),
                Paragraph(_format_currency(gross_subtotal), small_right),
            ],
            [
                Paragraph(_english_markup("Discount"), small_style),
                Paragraph(_format_currency(-total_disc if total_disc > 0 else 0.0), small_right),
            ],
            [
                Paragraph(_english_markup("Taxable Value", bold=True), small_style),
                Paragraph(_format_currency(total_taxable, bold=True), small_right),
            ],
            [
                Paragraph(_english_markup("CGST"), small_style),
                Paragraph(_format_currency(total_cgst), small_right),
            ],
            [
                Paragraph(_english_markup("SGST"), small_style),
                Paragraph(_format_currency(total_sgst), small_right),
            ],
        ]
        if total_igst > 0.004:
            totals_rows.append(
                [
                    Paragraph(_english_markup("IGST"), small_style),
                    Paragraph(_format_currency(total_igst), small_right),
                ]
            )
        if extra_charges > 0.004:
            totals_rows.append(
                [
                    Paragraph(_english_markup("Extra Charges"), small_style),
                    Paragraph(_format_currency(extra_charges), small_right),
                ]
            )
        totals_rows.append(
            [
                Paragraph(_english_markup("Round Off"), small_style),
                Paragraph(_format_currency(round_off), small_right),
            ]
        )

        grand_row_idx = len(totals_rows)
        totals_rows.append(
            [
                Paragraph(
                    _english_markup("Grand Total", bold=True),
                    ParagraphStyle("GTL", parent=small_style, fontName=FONT_ENGLISH_BOLD, fontSize=8.5, textColor=brand_accent),
                ),
                Paragraph(
                    _format_currency(net_amount, bold=True),
                    ParagraphStyle("GTR", parent=small_right, fontName=FONT_ENGLISH_BOLD, fontSize=8.5, textColor=brand_accent),
                ),
            ]
        )
        totals_rows.append(
            [
                Paragraph(_english_markup("Paid Amount"), small_style),
                Paragraph(_format_currency(paid_amount), small_right),
            ]
        )
        due_color_hex = "#b91c1c" if effective_due > 0.01 else "#15803d"
        totals_rows.append(
            [
                Paragraph(_english_markup("Balance Due", bold=True), small_style),
                Paragraph(
                    f"<font color='{due_color_hex}'>{_format_currency(effective_due, bold=True)}</font>",
                    small_right,
                ),
            ]
        )

        totals_table = Table(totals_rows, colWidths=[42 * mm, 36 * mm])
        totals_table.setStyle(
            TableStyle(
                [
                    ("BOX", (0, 0), (-1, -1), 0.7, border_dark),
                    ("INNERGRID", (0, 0), (-1, -1), 0.4, border_light),
                    ("BACKGROUND", (0, grand_row_idx), (-1, grand_row_idx), highlight_bg),
                    ("LINEABOVE", (0, grand_row_idx), (-1, grand_row_idx), 0.8, brand_accent),
                    ("LINEBELOW", (0, grand_row_idx), (-1, grand_row_idx), 0.8, brand_accent),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 2.5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )

        # Left column: Amount in Words, Bank Details (optional), Payment Terms, Customer Sign
        amount_words_str = self._number_to_words(net_amount)
        left_sections: List[Any] = [
            Paragraph(
                f"{_english_markup('Amount in Words:', bold=True)}<br/>{_english_markup(amount_words_str, bold=True)}",
                small_style,
            ),
            Spacer(1, 3 * mm),
        ]

        bank_name = (self.settings.bank_name or "").strip()
        acc_no = (self.settings.account_no or "").strip()
        ifsc = (self.settings.ifsc_code or "").strip()
        if bank_name or acc_no or ifsc:
            bank_bits: List[str] = []
            if bank_name:
                bank_bits.append(f"Bank: {bank_name}")
            if acc_no:
                bank_bits.append(f"A/c: {acc_no}")
            if ifsc:
                bank_bits.append(f"IFSC: {ifsc}")
            left_sections.append(
                Paragraph(
                    f"{_english_markup('Bank Details:', bold=True)} {_english_markup(' | '.join(bank_bits))}",
                    terms_style,
                )
            )
            left_sections.append(Spacer(1, 2.5 * mm))

        configured_terms = (self.settings.invoice_terms or "").strip()
        terms_text = (
            configured_terms
            if configured_terms
            else "1. Please verify batch number, expiry, and quantity at the time of delivery. 2. Store agricultural inputs safely away from food, children, and livestock."
        )
        left_sections.append(
            Paragraph(
                f"{_english_markup('Payment Terms & Notes:', bold=True)}<br/>{_english_markup(terms_text)}",
                terms_style,
            )
        )
        if (sale.remarks or "").strip():
            left_sections.append(Spacer(1, 2 * mm))
            left_sections.append(
                Paragraph(
                    f"{_english_markup('Remarks:', bold=True)} {_english_markup(sale.remarks.strip())}",
                    terms_style,
                )
            )

        left_sections.append(Spacer(1, 10 * mm))
        left_sections.append(
            Paragraph(
                _english_markup("Customer Signature", bold=True),
                small_style,
            )
        )

        # Right column: Totals Table + Authorized Signatory for Shop
        auth_p = Paragraph(
            f"{_english_markup('For ' + shop_name, bold=True)}<br/><br/><br/>{_english_markup('Authorized Signatory')}",
            ParagraphStyle("AuthSign", parent=small_right, leading=11),
        )
        right_column_flow: List[Any] = [totals_table, Spacer(1, 4 * mm), auth_p]

        bottom_table = Table(
            [[left_sections, right_column_flow]],
            colWidths=[112 * mm, 82 * mm],
        )
        bottom_table.setStyle(
            TableStyle(
                [
                    ("BOX", (0, 0), (-1, -1), 0.9, border_dark),
                    ("LINEBEFORE", (1, 0), (1, 0), 0.6, border_dark),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        story.append(bottom_table)

        # Jurisdiction note (strictly dynamic from shop city; no invented city claims)
        story.append(Spacer(1, 2.5 * mm))
        shop_city = (self.settings.city or "").strip()
        if shop_city:
            footer_text = f"Subject to {shop_city} Jurisdiction | Computer Generated Tax Invoice"
        else:
            footer_text = "Computer Generated Tax Invoice"
        story.append(Paragraph(_english_markup(footer_text), footer_center))

        doc.build(story)

        if output_path:
            with open(output_path, "rb") as f:
                return f.read()
        return buffer.getvalue()
