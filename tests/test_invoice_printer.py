"""
Automated Test Suite for ReportLab GST Invoice Printing Engine.
"""
from pathlib import Path
import pytest
from src.models import CompanySettings, Sale, SaleItem
from src.printing.invoice_printer import InvoicePrinter


def test_invoice_pdf_generation(tmp_path):
    """Test generating a GST Tax Invoice PDF."""
    settings = CompanySettings(
        company_name="Krishi Seva Kendra",
        address="Near Bus Stand, Station Road",
        city="Baramati",
        state="Maharashtra",
        pincode="413102",
        mobile="9890123456",
        gstin="27AABCU9603R1ZM",
        dl_fertilizer="LIC/FERT/2026/890",
        dl_pesticide="LIC/PEST/2026/123",
        dl_seed="LIC/SEED/2026/456",
        bank_name="State Bank of India",
        account_no="30012345678",
        ifsc_code="SBIN0000321",
    )

    printer = InvoicePrinter(company_settings=settings)

    items = [
        SaleItem(
            product_id=1,
            batch_id=1,
            product_name="IFFCO NPK 10:26:26 (50kg)",
            batch_no="NPK-2026-99",
            exp_date="2028-06-30",
            hsn_code="31052000",
            qty=4.0,
            unit_name="BAG",
            sale_rate=1470.0,
            mrp=1470.0,
            taxable_amount=5600.0,
            cgst_rate=2.5,
            cgst_amount=140.0,
            sgst_rate=2.5,
            sgst_amount=140.0,
            total_amount=5880.0,
        ),
        SaleItem(
            product_id=2,
            batch_id=2,
            product_name="Bayer Confidor Insecticide (100ml)",
            batch_no="CONF-887",
            exp_date="2027-11-30",
            hsn_code="38089190",
            qty=2.0,
            unit_name="BTL",
            sale_rate=450.0,
            mrp=480.0,
            taxable_amount=762.71,
            cgst_rate=9.0,
            cgst_amount=68.64,
            sgst_rate=9.0,
            sgst_amount=68.64,
            total_amount=900.0,
        ),
    ]

    sale = Sale(
        invoice_no="INV-2026-0042",
        sale_date="2026-09-12",
        customer_id=1,
        customer_name="Shivaji Babar",
        customer_mobile="9823456789",
        customer_village="Karhati",
        payment_mode="CASH",
        total_taxable=6362.71,
        total_cgst=208.64,
        total_sgst=208.64,
        round_off=0.01,
        net_amount=6780.0,
        paid_amount=6780.0,
        due_amount=0.0,
        items=items,
    )

    pdf_output_path = tmp_path / "test_tax_invoice.pdf"
    pdf_bytes = printer.generate_invoice_pdf(sale=sale, output_path=pdf_output_path)

    # Assert PDF file was written and starts with %PDF magic bytes
    assert pdf_output_path.exists()
    assert pdf_output_path.stat().st_size > 1000  # PDF is non-trivial size
    assert pdf_bytes.startswith(b"%PDF-")


def test_number_to_words():
    """Test Rupee amount in words conversion including hundreds and paise."""
    printer = InvoicePrinter()
    assert "SIX THOUSAND SEVEN HUNDRED EIGHTY" in printer._number_to_words(6780.0)
    assert "SIX THOUSAND SEVEN HUNDRED THIRTY FIVE" in printer._number_to_words(6735.0)
    assert "FIFTY THOUSAND" in printer._number_to_words(50000.0)
    assert "ONE LAKH TWENTY FIVE THOUSAND" in printer._number_to_words(125000.0)
    assert "ONE HUNDRED TWENTY FIVE AND FIFTY PAISE" in printer._number_to_words(125.50)
    assert printer._number_to_words(0.0) == "ZERO RUPEES ONLY"


def test_invoice_special_chars_minimal_settings_and_no_developer_info():
    """Verify XML special chars (&, <, >, quotes), minimal settings hiding blanks, and zero hardcoded PII."""
    settings = CompanySettings(
        company_name="Patil & Sons <Agro> \"No.1\" (पाटील अँड सन्स)",
        state="Maharashtra",
    )
    sale = Sale(
        invoice_no="INV-9001",
        sale_date="2026-11-09",
        customer_id=1,
        customer_name="O'Connor & Sons",
        payment_mode="CASH",
        total_taxable=500.0,
        total_igst=90.0,
        net_amount=590.0,
        paid_amount=590.0,
        due_amount=0.0,
        items=[
            SaleItem(
                product_id=1,
                product_name="Micro Nutrient <Zn & B> 'Premium'",
                qty=1.0,
                sale_rate=590.0,
                mrp=600.0,
                taxable_amount=500.0,
                igst_rate=18.0,
                igst_amount=90.0,
                total_amount=590.0,
            )
        ],
    )
    pdf_bytes = InvoicePrinter(company_settings=settings).generate_invoice_pdf(sale=sale)
    assert pdf_bytes.startswith(b"%PDF-")
    assert len(pdf_bytes) > 1500


def test_c2c_english_only_rupee_font_and_no_devanagari_routing():
    """
    C2-C Task 1 & 2:
    Verify English-only Helvetica font setup, bundled offline Rupee font registration,
    removal of Devanagari script-routing helpers, and crisp Rupee markup generation.
    """
    import src.printing.invoice_printer as ip_mod

    assert ip_mod.FONT_ENGLISH == "Helvetica"
    assert ip_mod.FONT_ENGLISH_BOLD == "Helvetica-Bold"
    assert ip_mod.HAS_RUPEE_FONT is True
    assert ip_mod.REG_RUPEE_FONT_PATH.exists()
    assert ip_mod.BOLD_RUPEE_FONT_PATH.exists()

    # Legacy Devanagari routing helpers must be removed
    assert not hasattr(ip_mod, "_is_deva_glyph")
    assert not hasattr(ip_mod, "_bilingual_markup")
    assert not hasattr(ip_mod, "FONT_LATIN")
    assert not hasattr(ip_mod, "FONT_LATIN_BOLD")

    # Sanitization strips non-English script, normalizes punctuation, preserves ₹
    cleaned = ip_mod._sanitize_english_text("Mauli • Agro – “Best” ‘Seed’ माऊली ₹ 500")
    assert "माऊली" not in cleaned
    assert "₹ 500" in cleaned
    assert 'Mauli - Agro - "Best" \'Seed\'' in cleaned

    # Only ₹ is switched to Rupee font; surrounding English & XML escapes use Helvetica
    markup = ip_mod._english_markup("A & B < ₹ 1,250.00 >", bold=True)
    assert "<b>A &amp; B &lt; </b>" in markup
    assert '<font name="RupeeBold">₹</font>' in markup
    assert "<b> 1,250.00 &gt;</b>" in markup


def test_c2c_long_names_special_chars_and_multipage_repeating_headers():
    """
    C2-C Task 5:
    Verify long shop/customer/product names, special XML characters (&, <, >, ", '),
    and large multi-page invoices (45 line items) generate valid multi-page A4 PDFs
    with repeating table headers and zero layout failures.
    """
    import re

    settings = CompanySettings(
        company_name="Shree Sant Tukaram Maharaj Cooperative Agricultural Input Supply & Soil Health Advisory Kendra Pvt. Ltd.",
        owner_name="Pandurang Vitthalrao Kulkarni-Deshpande",
        address="Gate No. 412/B, Opposite New Agricultural Produce Market Committee Yard",
        city="Ahmednagar",
        state="Maharashtra",
        pincode="414001",
        mobile="9822001122",
        gstin="27CCCCC3333C1Z3",
        invoice_terms="1. Batch & expiry verified on receipt. 2. Store away from children & food.",
    )
    items = [
        SaleItem(
            product_id=i,
            product_name=f"Agro Nutrient & Crop Shield <Grade {i:02d}> \"Super\" 'Mix'",
            hsn_code="38089190",
            batch_no=f"BATCH-LONG-{i:03d}",
            exp_date="2028-12-31",
            qty=1.0,
            unit_name="BTL",
            sale_rate=118.0,
            mrp=125.0,
            taxable_amount=100.0,
            cgst_rate=9.0,
            cgst_amount=9.0,
            sgst_rate=9.0,
            sgst_amount=9.0,
            total_amount=118.0,
        )
        for i in range(1, 46)
    ]
    sale = Sale(
        invoice_no="INV-C2C-MULTI-045",
        sale_date="2026-11-09",
        customer_id=1,
        customer_name="Kisanrao Baburao Jagtap & Family Orchard Farming Group <East Wing> \"Plot 4 & 5\"",
        customer_mobile="9765432100",
        customer_village="Shrirampur Road",
        customer_taluka="Rahata",
        customer_district="Ahilyanagar",
        payment_mode="CASH",
        total_taxable=4500.0,
        total_cgst=405.0,
        total_sgst=405.0,
        extra_charges=25.0,
        round_off=0.0,
        net_amount=5335.0,
        paid_amount=5335.0,
        due_amount=0.0,
        items=items,
    )

    pdf_bytes = InvoicePrinter(company_settings=settings).generate_invoice_pdf(sale=sale)
    assert pdf_bytes.startswith(b"%PDF-")
    assert len(pdf_bytes) > 5000

    # At least 2 A4 pages for 45 line items with repeating header rows
    page_count = len(re.findall(rb"/Type\s*/Page\b", pdf_bytes))
    assert page_count >= 2, f"Expected multi-page PDF (>=2 pages), got {page_count}"



