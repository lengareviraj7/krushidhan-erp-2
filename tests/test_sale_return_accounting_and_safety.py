"""
Comprehensive Isolated Test Suite for Phase 2B-2:
Sales Return Accounting and Transaction Safety.

Covers all 15 required verification areas against strictly isolated temporary SQLite databases:
1. Cash-sale (and Bank/UPI-sale) return with exact journal & cash/bank movement (no double-reduction of customer balance).
2. Credit-sale return reducing Sundry Debtors (NOT Sales Account) consistently with customer balance.
3. Partially paid sale returns (return <= due, return > due, and multi-return sequence).
4. CGST & SGST Output reversal strictly from original sale snapshot (immune to later product tax changes).
5. IGST Output reversal strictly from original sale snapshot (immune to later product tax changes).
6. Positive & negative round-off balancing and 100% return netting all accounts to zero.
7. Customer balance reconciliation with Sundry Debtors ledger across mixed workflows.
8. Stock restoration & stock_ledger consistency.
9. Over-return rejection (single, cumulative, and duplicate lines in one return request).
10. Forced accounting failure rolls back entire return atomically.
11. Forced stock failure rolls back entire return (including earlier lines) atomically.
12. Every repository operation inside process_sale_return() (including get_account_by_name) uses shared conn.
13. Exactly ONE SQLite connection is opened (and BEGIN IMMEDIATE mode is used).
14. Controlled multi-threaded concurrency tests proving concurrent requests cannot over-return.
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Dict, List

import pytest

from src.db.connection import DatabaseManager, reset_db_manager
from src.models.inventory import StockBatch
from src.models.master_data import Customer, Product, Supplier
from src.models.sales import Sale, SaleItem, SalesReturn, SalesReturnItem
from src.repositories.accounting_repository import AccountingRepository
from src.repositories.inventory_repository import InventoryRepository
from src.repositories.master_data_repository import MasterDataRepository
from src.repositories.sales_repository import SalesRepository
from src.services.accounting_service import AccountingService
from src.services.sales_service import SalesService


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Provides a strictly isolated file-based SQLite DB and initialized services."""
    db_path = tmp_path / "test_phase2b2_sale_returns.db"
    bkp_dir = tmp_path / "test_phase2b2_backups"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("BACKUP_DIR", str(bkp_dir))
    reset_db_manager()

    db = DatabaseManager(db_path=db_path, backup_dir=bkp_dir, clean_install=True)
    db.initialize_database(include_seed=True)

    master_repo = MasterDataRepository(db)
    inv_repo = InventoryRepository(db)
    sales_repo = SalesRepository(db)
    acc_repo = AccountingRepository(db)
    sales_svc = SalesService(db)

    cust_id = master_repo.create_customer(
        Customer(customer_name="Dnyaneshwar Shinde", mobile="9822334455", village="Nashik", opening_balance=0.0)
    )
    supp_id = master_repo.create_supplier(
        Supplier(supplier_name="RCF Distributors", gstin="27AAACR1234F1Z9", city="Mumbai", opening_balance=0.0)
    )
    prod1_id = master_repo.create_product(
        Product(
            product_name="Pesticide 18% GST",
            category_id=3,
            unit_id=1,
            tax_group_id=4,  # 18% (9% CGST + 9% SGST)
            default_purchase_rate=200.0,
            default_sale_rate=300.0,
            default_mrp=350.0,
        )
    )
    prod2_id = master_repo.create_product(
        Product(
            product_name="Fertilizer 5% GST",
            category_id=1,
            unit_id=2,
            tax_group_id=2,  # 5% (2.5% CGST + 2.5% SGST)
            default_purchase_rate=400.0,
            default_sale_rate=525.0,
            default_mrp=550.0,
        )
    )
    batch1_id = inv_repo.upsert_batch(
        StockBatch(
            product_id=prod1_id,
            batch_no="PEST-2026-A",
            mfg_date="2026-01-01",
            exp_date="2027-12-31",
            purchase_rate=200.0,
            mrp=350.0,
            sale_rate=300.0,
            current_qty=50.0,
        )
    )
    batch2_id = inv_repo.upsert_batch(
        StockBatch(
            product_id=prod2_id,
            batch_no="FERT-2026-B",
            mfg_date="2026-01-01",
            exp_date="2027-12-31",
            purchase_rate=400.0,
            mrp=550.0,
            sale_rate=525.0,
            current_qty=50.0,
        )
    )

    yield {
        "db": db,
        "master_repo": master_repo,
        "inv_repo": inv_repo,
        "sales_repo": sales_repo,
        "acc_repo": acc_repo,
        "sales_svc": sales_svc,
        "cust_id": cust_id,
        "supp_id": supp_id,
        "prod1_id": prod1_id,
        "prod2_id": prod2_id,
        "batch1_id": batch1_id,
        "batch2_id": batch2_id,
    }
    reset_db_manager()


def _get_return_voucher_entries_by_account(db: DatabaseManager, return_id: int) -> Dict[str, Dict[str, float]]:
    vch = db.fetch_one(
        "SELECT * FROM vouchers WHERE reference_type = 'SALE_RETURN' AND reference_id = ?;",
        (return_id,),
    )
    assert vch is not None
    rows = db.fetch_all(
        """
        SELECT la.account_name, le.debit_amount, le.credit_amount
        FROM ledger_entries le
        JOIN ledger_accounts la ON le.account_id = la.account_id
        WHERE le.voucher_id = ?;
        """,
        (vch["voucher_id"],),
    )
    result: Dict[str, Dict[str, float]] = {}
    for r in rows:
        name = r["account_name"]
        if name not in result:
            result[name] = {"dr": 0.0, "cr": 0.0}
        result[name]["dr"] = round(result[name]["dr"] + float(r["debit_amount"]), 2)
        result[name]["cr"] = round(result[name]["cr"] + float(r["credit_amount"]), 2)
    tot_dr = round(sum(v["dr"] for v in result.values()), 2)
    tot_cr = round(sum(v["cr"] for v in result.values()), 2)
    assert tot_dr == tot_cr
    return result


# ---------------------------------------------------------------------------
# 1. Cash-sale and UPI/Bank-sale return
# ---------------------------------------------------------------------------

def test_cash_sale_return_refunds_cash_without_touching_customer_balance(env):
    db = env["db"]
    sales_svc = env["sales_svc"]
    master_repo = env["master_repo"]
    acc_repo = env["acc_repo"]

    sale_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-CASH-01",
            sale_date="2026-11-17",
            customer_id=env["cust_id"],
            payment_mode="CASH",
            total_taxable=1000.0,
            total_cgst=90.0,
            total_sgst=90.0,
            net_amount=1180.0,
            paid_amount=1180.0,
            due_amount=0.0,
            items=[
                SaleItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=4.0,
                    sale_rate=295.0,
                    mrp=350.0,
                    taxable_amount=1000.0,
                    cgst_rate=9.0,
                    cgst_amount=90.0,
                    sgst_rate=9.0,
                    sgst_amount=90.0,
                    total_amount=1180.0,
                )
            ],
        )
    )

    assert acc_repo.get_cash_account().current_balance == pytest.approx(1180.0)
    assert acc_repo.get_debtors_account().current_balance == pytest.approx(0.0)
    assert master_repo.get_customer_by_id(env["cust_id"]).current_balance == pytest.approx(0.0)

    ret_id = sales_svc.process_sale_return(
        SalesReturn(
            return_no="RET-CASH-01",
            return_date="2026-11-17",
            sale_id=sale_id,
            customer_id=env["cust_id"],
            total_taxable=500.0,
            total_tax=90.0,
            net_amount=590.0,
            items=[
                SalesReturnItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=2.0,
                    sale_rate=295.0,
                    taxable_amount=500.0,
                    tax_amount=90.0,
                    total_amount=590.0,
                )
            ],
        )
    )

    entries = _get_return_voucher_entries_by_account(db, ret_id)
    assert entries == {
        "Sales Return Account": {"dr": 500.0, "cr": 0.0},
        "CGST Output Account": {"dr": 45.0, "cr": 0.0},
        "SGST Output Account": {"dr": 45.0, "cr": 0.0},
        "Cash in Hand": {"dr": 0.0, "cr": 590.0},
    }
    # Cash reduced from 1180 -> 590; Sundry Debtors & customer balance remain 0.0 (never double-credited!)
    assert acc_repo.get_cash_account().current_balance == pytest.approx(590.0)
    assert acc_repo.get_debtors_account().current_balance == pytest.approx(0.0)
    assert master_repo.get_customer_by_id(env["cust_id"]).current_balance == pytest.approx(0.0)


def test_upi_sale_return_refunds_bank_account_without_touching_customer_balance(env):
    db = env["db"]
    sales_svc = env["sales_svc"]
    master_repo = env["master_repo"]
    acc_repo = env["acc_repo"]

    sale_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-UPI-01",
            sale_date="2026-11-17",
            customer_id=env["cust_id"],
            payment_mode="UPI",
            total_taxable=500.0,
            total_cgst=45.0,
            total_sgst=45.0,
            net_amount=590.0,
            paid_amount=590.0,
            due_amount=0.0,
            items=[
                SaleItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=2.0,
                    sale_rate=295.0,
                    mrp=350.0,
                    taxable_amount=500.0,
                    cgst_rate=9.0,
                    cgst_amount=45.0,
                    sgst_rate=9.0,
                    sgst_amount=45.0,
                    total_amount=590.0,
                )
            ],
        )
    )

    ret_id = sales_svc.process_sale_return(
        SalesReturn(
            return_no="RET-UPI-01",
            return_date="2026-11-17",
            sale_id=sale_id,
            customer_id=env["cust_id"],
            total_taxable=250.0,
            total_tax=45.0,
            net_amount=295.0,
            items=[
                SalesReturnItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=1.0,
                    sale_rate=295.0,
                    taxable_amount=250.0,
                    tax_amount=45.0,
                    total_amount=295.0,
                )
            ],
        )
    )

    entries = _get_return_voucher_entries_by_account(db, ret_id)
    assert entries == {
        "Sales Return Account": {"dr": 250.0, "cr": 0.0},
        "CGST Output Account": {"dr": 22.5, "cr": 0.0},
        "SGST Output Account": {"dr": 22.5, "cr": 0.0},
        "Bank Account": {"dr": 0.0, "cr": 295.0},
    }
    assert acc_repo.get_bank_account().current_balance == pytest.approx(295.0)
    assert acc_repo.get_cash_account().current_balance == pytest.approx(0.0)
    assert master_repo.get_customer_by_id(env["cust_id"]).current_balance == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 2. Credit-sale return reducing Sundry Debtors (NOT Sales Account)
# ---------------------------------------------------------------------------

def test_credit_sale_return_credits_sundry_debtors_not_sales_account(env):
    db = env["db"]
    sales_svc = env["sales_svc"]
    master_repo = env["master_repo"]
    acc_repo = env["acc_repo"]

    sale_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-CRED-01",
            sale_date="2026-11-17",
            customer_id=env["cust_id"],
            payment_mode="CREDIT",
            total_taxable=1000.0,
            total_cgst=90.0,
            total_sgst=90.0,
            net_amount=1180.0,
            paid_amount=0.0,
            due_amount=1180.0,
            items=[
                SaleItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=4.0,
                    sale_rate=295.0,
                    mrp=350.0,
                    taxable_amount=1000.0,
                    cgst_rate=9.0,
                    cgst_amount=90.0,
                    sgst_rate=9.0,
                    sgst_amount=90.0,
                    total_amount=1180.0,
                )
            ],
        )
    )

    ret_id = sales_svc.process_sale_return(
        SalesReturn(
            return_no="RET-CRED-01",
            return_date="2026-11-17",
            sale_id=sale_id,
            customer_id=env["cust_id"],
            total_taxable=250.0,
            total_tax=45.0,
            net_amount=295.0,
            items=[
                SalesReturnItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=1.0,
                    sale_rate=295.0,
                    taxable_amount=250.0,
                    tax_amount=45.0,
                    total_amount=295.0,
                )
            ],
        )
    )

    entries = _get_return_voucher_entries_by_account(db, ret_id)
    assert "Sales Account" not in entries
    assert entries == {
        "Sales Return Account": {"dr": 250.0, "cr": 0.0},
        "CGST Output Account": {"dr": 22.5, "cr": 0.0},
        "SGST Output Account": {"dr": 22.5, "cr": 0.0},
        "Sundry Debtors": {"dr": 0.0, "cr": 295.0},
    }
    # Customer balance and Sundry Debtors match identically (1180 - 295 = 885)
    assert acc_repo.get_debtors_account().current_balance == pytest.approx(885.0)
    assert master_repo.get_customer_by_id(env["cust_id"]).current_balance == pytest.approx(885.0)
    assert acc_repo.get_sales_account().current_balance == pytest.approx(1000.0)
    assert acc_repo.get_cash_account().current_balance == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 3 & 7. Partially paid sale returns (split receivable vs refund + reconciliation)
# ---------------------------------------------------------------------------

def test_partially_paid_sale_return_reduces_receivable_first_then_refunds_excess(env):
    db = env["db"]
    sales_svc = env["sales_svc"]
    master_repo = env["master_repo"]
    acc_repo = env["acc_repo"]

    # Sale: net 1180, paid 700 CASH, due 480 -> Cash=700, Debtors=480, Customer=480
    sale_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-PART-01",
            sale_date="2026-11-17",
            customer_id=env["cust_id"],
            payment_mode="CASH",
            total_taxable=1000.0,
            total_cgst=90.0,
            total_sgst=90.0,
            net_amount=1180.0,
            paid_amount=700.0,
            due_amount=480.0,
            items=[
                SaleItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=4.0,
                    sale_rate=295.0,
                    mrp=350.0,
                    taxable_amount=1000.0,
                    cgst_rate=9.0,
                    cgst_amount=90.0,
                    sgst_rate=9.0,
                    sgst_amount=90.0,
                    total_amount=1180.0,
                )
            ],
        )
    )

    # Return #1: 1 unit = 295 (<= due 480) -> ALL 295 reduces Sundry Debtors, 0 cash refund
    ret1_id = sales_svc.process_sale_return(
        SalesReturn(
            return_no="RET-PART-01",
            return_date="2026-11-17",
            sale_id=sale_id,
            customer_id=env["cust_id"],
            total_taxable=250.0,
            total_tax=45.0,
            net_amount=295.0,
            items=[
                SalesReturnItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=1.0,
                    sale_rate=295.0,
                    taxable_amount=250.0,
                    tax_amount=45.0,
                    total_amount=295.0,
                )
            ],
        )
    )
    entries1 = _get_return_voucher_entries_by_account(db, ret1_id)
    assert entries1 == {
        "Sales Return Account": {"dr": 250.0, "cr": 0.0},
        "CGST Output Account": {"dr": 22.5, "cr": 0.0},
        "SGST Output Account": {"dr": 22.5, "cr": 0.0},
        "Sundry Debtors": {"dr": 0.0, "cr": 295.0},
    }
    assert acc_repo.get_debtors_account().current_balance == pytest.approx(185.0)
    assert master_repo.get_customer_by_id(env["cust_id"]).current_balance == pytest.approx(185.0)
    assert acc_repo.get_cash_account().current_balance == pytest.approx(700.0)

    # Return #2: 2 units = 590 (> remaining due 185) -> 185 reduces Debtors to 0, 405 refunded in Cash!
    ret2_id = sales_svc.process_sale_return(
        SalesReturn(
            return_no="RET-PART-02",
            return_date="2026-11-17",
            sale_id=sale_id,
            customer_id=env["cust_id"],
            total_taxable=500.0,
            total_tax=90.0,
            net_amount=590.0,
            items=[
                SalesReturnItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=2.0,
                    sale_rate=295.0,
                    taxable_amount=500.0,
                    tax_amount=90.0,
                    total_amount=590.0,
                )
            ],
        )
    )
    entries2 = _get_return_voucher_entries_by_account(db, ret2_id)
    assert entries2 == {
        "Sales Return Account": {"dr": 500.0, "cr": 0.0},
        "CGST Output Account": {"dr": 45.0, "cr": 0.0},
        "SGST Output Account": {"dr": 45.0, "cr": 0.0},
        "Sundry Debtors": {"dr": 0.0, "cr": 185.0},
        "Cash in Hand": {"dr": 0.0, "cr": 405.0},
    }
    # Final reconciliation: Customer kept 1 unit (295), owes 0, net cash kept by shop = 700 - 405 = 295!
    assert acc_repo.get_debtors_account().current_balance == pytest.approx(0.0)
    assert master_repo.get_customer_by_id(env["cust_id"]).current_balance == pytest.approx(0.0)
    assert acc_repo.get_cash_account().current_balance == pytest.approx(295.0)


# ---------------------------------------------------------------------------
# 4 & 5. CGST/SGST and IGST reversal strictly from original sale snapshot
# ---------------------------------------------------------------------------

def test_cgst_sgst_reversal_ignores_later_product_tax_group_mutation(env):
    db = env["db"]
    sales_svc = env["sales_svc"]
    acc_repo = env["acc_repo"]

    # Sale recorded at 18% (9% CGST + 9% SGST)
    sale_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-CGST-HIST",
            sale_date="2026-11-17",
            customer_id=env["cust_id"],
            payment_mode="CREDIT",
            total_taxable=1000.0,
            total_cgst=90.0,
            total_sgst=90.0,
            total_igst=0.0,
            net_amount=1180.0,
            paid_amount=0.0,
            due_amount=1180.0,
            items=[
                SaleItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=2.0,
                    sale_rate=590.0,
                    mrp=600.0,
                    taxable_amount=1000.0,
                    cgst_rate=9.0,
                    cgst_amount=90.0,
                    sgst_rate=9.0,
                    sgst_amount=90.0,
                    igst_rate=0.0,
                    igst_amount=0.0,
                    total_amount=1180.0,
                )
            ],
        )
    )

    # Mutate product tax group AFTER the sale to 0% Exempted (tax_group_id=1)
    with db.transaction() as conn:
        conn.execute("UPDATE products SET tax_group_id = 1 WHERE product_id = ?;", (env["prod1_id"],))

    ret_id = sales_svc.process_sale_return(
        SalesReturn(
            return_no="RET-CGST-HIST",
            return_date="2026-11-17",
            sale_id=sale_id,
            customer_id=env["cust_id"],
            total_taxable=500.0,
            total_tax=90.0,
            net_amount=590.0,
            items=[
                SalesReturnItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=1.0,
                    sale_rate=590.0,
                    taxable_amount=500.0,
                    tax_amount=90.0,
                    total_amount=590.0,
                )
            ],
        )
    )

    entries = _get_return_voucher_entries_by_account(db, ret_id)
    assert entries["CGST Output Account"] == {"dr": 45.0, "cr": 0.0}
    assert entries["SGST Output Account"] == {"dr": 45.0, "cr": 0.0}
    assert "IGST Output Account" not in entries
    assert acc_repo.get_output_gst_account("CGST").current_balance == pytest.approx(45.0)
    assert acc_repo.get_output_gst_account("SGST").current_balance == pytest.approx(45.0)


def test_igst_reversal_ignores_later_product_tax_group_mutation(env):
    db = env["db"]
    sales_svc = env["sales_svc"]
    acc_repo = env["acc_repo"]

    # Inter-state sale recorded with 18% IGST
    sale_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-IGST-01",
            sale_date="2026-11-17",
            customer_id=env["cust_id"],
            payment_mode="CREDIT",
            total_taxable=1000.0,
            total_cgst=0.0,
            total_sgst=0.0,
            total_igst=180.0,
            net_amount=1180.0,
            paid_amount=0.0,
            due_amount=1180.0,
            items=[
                SaleItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=2.0,
                    sale_rate=590.0,
                    mrp=600.0,
                    taxable_amount=1000.0,
                    cgst_rate=0.0,
                    cgst_amount=0.0,
                    sgst_rate=0.0,
                    sgst_amount=0.0,
                    igst_rate=18.0,
                    igst_amount=180.0,
                    total_amount=1180.0,
                )
            ],
        )
    )

    # Change product tax group to 5% AFTER sale
    with db.transaction() as conn:
        conn.execute("UPDATE products SET tax_group_id = 2 WHERE product_id = ?;", (env["prod1_id"],))

    ret_id = sales_svc.process_sale_return(
        SalesReturn(
            return_no="RET-IGST-01",
            return_date="2026-11-17",
            sale_id=sale_id,
            customer_id=env["cust_id"],
            total_taxable=500.0,
            total_tax=90.0,
            net_amount=590.0,
            items=[
                SalesReturnItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=1.0,
                    sale_rate=590.0,
                    taxable_amount=500.0,
                    tax_amount=90.0,
                    total_amount=590.0,
                )
            ],
        )
    )

    entries = _get_return_voucher_entries_by_account(db, ret_id)
    assert entries == {
        "Sales Return Account": {"dr": 500.0, "cr": 0.0},
        "IGST Output Account": {"dr": 90.0, "cr": 0.0},
        "Sundry Debtors": {"dr": 0.0, "cr": 590.0},
    }
    assert acc_repo.get_output_gst_account("IGST").current_balance == pytest.approx(90.0)
    assert acc_repo.get_output_gst_account("CGST").current_balance == pytest.approx(0.0)
    assert acc_repo.get_output_gst_account("SGST").current_balance == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 6. Positive & negative round-off balancing + full 100% return netting
# ---------------------------------------------------------------------------

def test_round_off_positive_and_negative_full_return_nets_all_accounts_to_zero(env):
    db = env["db"]
    sales_svc = env["sales_svc"]
    acc_repo = env["acc_repo"]

    # Positive round-off sale: taxable 2542.37 + cgst 228.81 + sgst 228.81 = 2999.99 -> net 3000.00 (+0.01)
    sale_pos_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-RND-POS",
            sale_date="2026-11-17",
            customer_id=env["cust_id"],
            payment_mode="CREDIT",
            total_taxable=2542.37,
            total_cgst=228.81,
            total_sgst=228.81,
            round_off=0.01,
            net_amount=3000.0,
            paid_amount=0.0,
            due_amount=3000.0,
            items=[
                SaleItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=10.0,
                    sale_rate=300.0,
                    mrp=350.0,
                    taxable_amount=2542.37,
                    cgst_rate=9.0,
                    cgst_amount=228.81,
                    sgst_rate=9.0,
                    sgst_amount=228.81,
                    total_amount=3000.0,
                )
            ],
        )
    )
    ret_pos_id = sales_svc.process_sale_return(
        SalesReturn(
            return_no="RET-RND-POS",
            return_date="2026-11-17",
            sale_id=sale_pos_id,
            customer_id=env["cust_id"],
            total_taxable=2542.37,
            total_tax=457.62,
            round_off=0.01,
            net_amount=3000.0,
            items=[
                SalesReturnItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=10.0,
                    sale_rate=300.0,
                    taxable_amount=2542.37,
                    tax_amount=457.62,
                    total_amount=3000.0,
                )
            ],
        )
    )
    entries_pos = _get_return_voucher_entries_by_account(db, ret_pos_id)
    assert entries_pos["Round Off Account"] == {"dr": 0.01, "cr": 0.0}
    assert acc_repo.get_output_gst_account("CGST").current_balance == pytest.approx(0.0)
    assert acc_repo.get_output_gst_account("SGST").current_balance == pytest.approx(0.0)
    assert acc_repo.get_round_off_account().current_balance == pytest.approx(0.0)
    assert acc_repo.get_debtors_account().current_balance == pytest.approx(0.0)

    # Negative round-off sale: taxable 100.34 + cgst 9.03 + sgst 9.03 = 118.40 -> net 118.00 (-0.40)
    sale_neg_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-RND-NEG",
            sale_date="2026-11-17",
            customer_id=env["cust_id"],
            payment_mode="CASH",
            total_taxable=100.34,
            total_cgst=9.03,
            total_sgst=9.03,
            round_off=-0.40,
            net_amount=118.0,
            paid_amount=118.0,
            due_amount=0.0,
            items=[
                SaleItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=1.0,
                    sale_rate=118.0,
                    mrp=120.0,
                    taxable_amount=100.34,
                    cgst_rate=9.0,
                    cgst_amount=9.03,
                    sgst_rate=9.0,
                    sgst_amount=9.03,
                    total_amount=118.0,
                )
            ],
        )
    )
    ret_neg_id = sales_svc.process_sale_return(
        SalesReturn(
            return_no="RET-RND-NEG",
            return_date="2026-11-17",
            sale_id=sale_neg_id,
            customer_id=env["cust_id"],
            total_taxable=100.34,
            total_tax=18.06,
            round_off=-0.40,
            net_amount=118.0,
            items=[
                SalesReturnItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=1.0,
                    sale_rate=118.0,
                    taxable_amount=100.34,
                    tax_amount=18.06,
                    total_amount=118.0,
                )
            ],
        )
    )
    entries_neg = _get_return_voucher_entries_by_account(db, ret_neg_id)
    assert entries_neg["Round Off Account"] == {"dr": 0.0, "cr": 0.40}
    assert acc_repo.get_output_gst_account("CGST").current_balance == pytest.approx(0.0)
    assert acc_repo.get_output_gst_account("SGST").current_balance == pytest.approx(0.0)
    assert acc_repo.get_round_off_account().current_balance == pytest.approx(0.0)
    assert acc_repo.get_cash_account().current_balance == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 8 & 9. Stock consistency & over-return blocking (including duplicate lines in 1 request)
# ---------------------------------------------------------------------------

def test_stock_restoration_and_duplicate_line_over_return_rejected(env):
    db = env["db"]
    sales_svc = env["sales_svc"]
    inv_repo = env["inv_repo"]

    sale_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-DUP-01",
            sale_date="2026-11-17",
            customer_id=env["cust_id"],
            payment_mode="CASH",
            total_taxable=500.0,
            total_cgst=45.0,
            total_sgst=45.0,
            net_amount=590.0,
            paid_amount=590.0,
            due_amount=0.0,
            items=[
                SaleItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=5.0,
                    sale_rate=118.0,
                    mrp=150.0,
                    taxable_amount=500.0,
                    cgst_rate=9.0,
                    cgst_amount=45.0,
                    sgst_rate=9.0,
                    sgst_amount=45.0,
                    total_amount=590.0,
                )
            ],
        )
    )
    assert inv_repo.get_batch_by_id(env["batch1_id"]).current_qty == pytest.approx(45.0)

    # Single request with 2 lines for same product totaling 6 > 5 sold MUST be rejected atomically
    with pytest.raises(ValueError, match="exceeds eligible"):
        sales_svc.process_sale_return(
            SalesReturn(
                return_no="RET-DUP-FAIL",
                return_date="2026-11-17",
                sale_id=sale_id,
                customer_id=env["cust_id"],
                net_amount=708.0,
                items=[
                    SalesReturnItem(
                        product_id=env["prod1_id"],
                        batch_id=env["batch1_id"],
                        qty=3.0,
                        sale_rate=118.0,
                        taxable_amount=300.0,
                        tax_amount=54.0,
                        total_amount=354.0,
                    ),
                    SalesReturnItem(
                        product_id=env["prod1_id"],
                        batch_id=env["batch1_id"],
                        qty=3.0,
                        sale_rate=118.0,
                        taxable_amount=300.0,
                        tax_amount=54.0,
                        total_amount=354.0,
                    ),
                ],
            )
        )

    assert inv_repo.get_batch_by_id(env["batch1_id"]).current_qty == pytest.approx(45.0)
    assert db.fetch_one("SELECT COUNT(*) as c FROM sales_returns;")["c"] == 0

    # Valid return of 2 units restores batch & logs stock_ledger
    ret_id = sales_svc.process_sale_return(
        SalesReturn(
            return_no="RET-OK-01",
            return_date="2026-11-17",
            sale_id=sale_id,
            customer_id=env["cust_id"],
            total_taxable=200.0,
            total_tax=36.0,
            net_amount=236.0,
            items=[
                SalesReturnItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=2.0,
                    sale_rate=118.0,
                    taxable_amount=200.0,
                    tax_amount=36.0,
                    total_amount=236.0,
                )
            ],
        )
    )
    assert ret_id > 0
    assert inv_repo.get_batch_by_id(env["batch1_id"]).current_qty == pytest.approx(47.0)
    assert inv_repo.get_product_total_stock(env["prod1_id"]) == pytest.approx(47.0)
    sl_rows = db.fetch_all("SELECT * FROM stock_ledger WHERE transaction_type = 'SALE_RETURN';")
    assert len(sl_rows) == 1
    assert sl_rows[0]["qty_in"] == pytest.approx(2.0)
    assert sl_rows[0]["balance_qty"] == pytest.approx(47.0)


# ---------------------------------------------------------------------------
# 10 & 11. Forced accounting failure & forced stock failure full atomic rollback
# ---------------------------------------------------------------------------

def test_forced_accounting_failure_rolls_back_entire_return(env, monkeypatch):
    db = env["db"]
    sales_svc = env["sales_svc"]
    inv_repo = env["inv_repo"]
    master_repo = env["master_repo"]

    sale_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-RB-ACC",
            sale_date="2026-11-17",
            customer_id=env["cust_id"],
            payment_mode="CREDIT",
            total_taxable=500.0,
            total_cgst=45.0,
            total_sgst=45.0,
            net_amount=590.0,
            paid_amount=0.0,
            due_amount=590.0,
            items=[
                SaleItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=2.0,
                    sale_rate=295.0,
                    mrp=350.0,
                    taxable_amount=500.0,
                    cgst_rate=9.0,
                    cgst_amount=45.0,
                    sgst_rate=9.0,
                    sgst_amount=45.0,
                    total_amount=590.0,
                )
            ],
        )
    )

    vch_count_before = db.fetch_one("SELECT COUNT(*) as c FROM vouchers;")["c"]
    sl_count_before = db.fetch_one("SELECT COUNT(*) as c FROM stock_ledger;")["c"]

    def _fail_voucher(*args, **kwargs):
        raise RuntimeError("Forced accounting voucher failure")

    monkeypatch.setattr(sales_svc.accounting_repo, "create_voucher", _fail_voucher)

    with pytest.raises(RuntimeError, match="Forced accounting voucher failure"):
        sales_svc.process_sale_return(
            SalesReturn(
                return_no="RET-RB-ACC",
                return_date="2026-11-17",
                sale_id=sale_id,
                customer_id=env["cust_id"],
                total_taxable=250.0,
                total_tax=45.0,
                net_amount=295.0,
                items=[
                    SalesReturnItem(
                        product_id=env["prod1_id"],
                        batch_id=env["batch1_id"],
                        qty=1.0,
                        sale_rate=295.0,
                        taxable_amount=250.0,
                        tax_amount=45.0,
                        total_amount=295.0,
                    )
                ],
            )
        )

    assert db.fetch_one("SELECT COUNT(*) as c FROM sales_returns;")["c"] == 0
    assert db.fetch_one("SELECT COUNT(*) as c FROM sales_return_items;")["c"] == 0
    assert db.fetch_one("SELECT COUNT(*) as c FROM vouchers;")["c"] == vch_count_before
    assert db.fetch_one("SELECT COUNT(*) as c FROM stock_ledger;")["c"] == sl_count_before
    assert inv_repo.get_batch_by_id(env["batch1_id"]).current_qty == pytest.approx(48.0)
    assert master_repo.get_customer_by_id(env["cust_id"]).current_balance == pytest.approx(590.0)


def test_forced_stock_failure_on_second_line_rolls_back_all_changes(env, monkeypatch):
    db = env["db"]
    sales_svc = env["sales_svc"]
    inv_repo = env["inv_repo"]
    master_repo = env["master_repo"]

    sale_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-RB-STK",
            sale_date="2026-11-17",
            customer_id=env["cust_id"],
            payment_mode="CREDIT",
            total_taxable=750.0,
            total_cgst=35.0,
            total_sgst=35.0,
            net_amount=820.0,
            paid_amount=0.0,
            due_amount=820.0,
            items=[
                SaleItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=1.0,
                    sale_rate=295.0,
                    mrp=350.0,
                    taxable_amount=250.0,
                    cgst_rate=9.0,
                    cgst_amount=22.5,
                    sgst_rate=9.0,
                    sgst_amount=22.5,
                    total_amount=295.0,
                ),
                SaleItem(
                    product_id=env["prod2_id"],
                    batch_id=env["batch2_id"],
                    qty=1.0,
                    sale_rate=525.0,
                    mrp=550.0,
                    taxable_amount=500.0,
                    cgst_rate=2.5,
                    cgst_amount=12.5,
                    sgst_rate=2.5,
                    sgst_amount=12.5,
                    total_amount=525.0,
                ),
            ],
        )
    )

    orig_add = sales_svc.inventory_repo.add_batch_stock
    call_idx = {"n": 0}

    def _fail_on_second_stock_update(batch_id, qty, conn=None):
        call_idx["n"] += 1
        if call_idx["n"] == 2:
            raise RuntimeError("Forced stock failure on line 2")
        return orig_add(batch_id, qty, conn=conn)

    monkeypatch.setattr(sales_svc.inventory_repo, "add_batch_stock", _fail_on_second_stock_update)

    vch_count_before = db.fetch_one("SELECT COUNT(*) as c FROM vouchers;")["c"]
    sl_count_before = db.fetch_one("SELECT COUNT(*) as c FROM stock_ledger;")["c"]

    with pytest.raises(RuntimeError, match="Forced stock failure on line 2"):
        sales_svc.process_sale_return(
            SalesReturn(
                return_no="RET-RB-STK",
                return_date="2026-11-17",
                sale_id=sale_id,
                customer_id=env["cust_id"],
                total_taxable=750.0,
                total_tax=70.0,
                net_amount=820.0,
                items=[
                    SalesReturnItem(
                        product_id=env["prod1_id"],
                        batch_id=env["batch1_id"],
                        qty=1.0,
                        sale_rate=295.0,
                        taxable_amount=250.0,
                        tax_amount=45.0,
                        total_amount=295.0,
                    ),
                    SalesReturnItem(
                        product_id=env["prod2_id"],
                        batch_id=env["batch2_id"],
                        qty=1.0,
                        sale_rate=525.0,
                        taxable_amount=500.0,
                        tax_amount=25.0,
                        total_amount=525.0,
                    ),
                ],
            )
        )

    # First batch stock update MUST ALSO have been rolled back!
    assert inv_repo.get_batch_by_id(env["batch1_id"]).current_qty == pytest.approx(49.0)
    assert inv_repo.get_batch_by_id(env["batch2_id"]).current_qty == pytest.approx(49.0)
    assert db.fetch_one("SELECT COUNT(*) as c FROM sales_returns;")["c"] == 0
    assert db.fetch_one("SELECT COUNT(*) as c FROM sales_return_items;")["c"] == 0
    assert db.fetch_one("SELECT COUNT(*) as c FROM vouchers;")["c"] == vch_count_before
    assert db.fetch_one("SELECT COUNT(*) as c FROM stock_ledger;")["c"] == sl_count_before
    assert master_repo.get_customer_by_id(env["cust_id"]).current_balance == pytest.approx(820.0)


# ---------------------------------------------------------------------------
# 12 & 13. Shared single connection & BEGIN IMMEDIATE enforcement
# ---------------------------------------------------------------------------

def test_single_immediate_connection_and_every_repo_call_uses_shared_conn(env, monkeypatch):
    db = env["db"]
    sales_svc = env["sales_svc"]

    sale_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-CONN-01",
            sale_date="2026-11-17",
            customer_id=env["cust_id"],
            payment_mode="CREDIT",
            total_taxable=500.0,
            total_cgst=45.0,
            total_sgst=45.0,
            net_amount=590.0,
            paid_amount=0.0,
            due_amount=590.0,
            items=[
                SaleItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=2.0,
                    sale_rate=295.0,
                    mrp=350.0,
                    taxable_amount=500.0,
                    cgst_rate=9.0,
                    cgst_amount=45.0,
                    sgst_rate=9.0,
                    sgst_amount=45.0,
                    total_amount=590.0,
                )
            ],
        )
    )

    tx_modes: List[str] = []
    orig_tx = db.transaction

    def _spy_tx(mode="DEFERRED"):
        tx_modes.append(mode)
        return orig_tx(mode=mode)

    monkeypatch.setattr(db, "transaction", _spy_tx)

    opened_conns: List[sqlite3.Connection] = []
    orig_get_conn = db.get_connection

    def _spy_get_conn(*args, **kwargs):
        c = orig_get_conn(*args, **kwargs)
        opened_conns.append(c)
        return c

    monkeypatch.setattr(db, "get_connection", _spy_get_conn)

    # Spy on every repository method invoked during process_sale_return()
    repo_conns: Dict[str, List[sqlite3.Connection]] = {}

    def _wrap(obj, attr_name):
        orig = getattr(obj, attr_name)
        repo_conns[attr_name] = []

        def _wrapped(*args, **kwargs):
            repo_conns[attr_name].append(kwargs.get("conn"))
            return orig(*args, **kwargs)

        monkeypatch.setattr(obj, attr_name, _wrapped)

    _wrap(sales_svc.sales_repo, "get_sale_by_id")
    _wrap(sales_svc.sales_repo, "get_already_returned_qty_map")
    _wrap(sales_svc.sales_repo, "get_already_returned_qty_by_batch_map")
    _wrap(sales_svc.sales_repo, "get_prior_returned_amount")
    _wrap(sales_svc.sales_repo, "get_prior_refunded_amount")
    _wrap(sales_svc.sales_repo, "create_sale_return")
    _wrap(sales_svc.master_repo, "get_customer_by_id")
    _wrap(sales_svc.master_repo, "update_customer_balance")
    _wrap(sales_svc.inventory_repo, "get_batch_by_id")
    _wrap(sales_svc.inventory_repo, "add_batch_stock")
    _wrap(sales_svc.inventory_repo, "get_product_total_stock")
    _wrap(sales_svc.inventory_repo, "record_stock_movement")
    _wrap(sales_svc.accounting_repo, "get_account_by_name")
    _wrap(sales_svc.accounting_repo, "generate_next_voucher_no")
    _wrap(sales_svc.accounting_repo, "create_voucher")

    ret_id = sales_svc.process_sale_return(
        SalesReturn(
            return_no="RET-CONN-01",
            return_date="2026-11-17",
            sale_id=sale_id,
            customer_id=env["cust_id"],
            total_taxable=250.0,
            total_tax=45.0,
            net_amount=295.0,
            items=[
                SalesReturnItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=1.0,
                    sale_rate=295.0,
                    taxable_amount=250.0,
                    tax_amount=45.0,
                    total_amount=295.0,
                )
            ],
        )
    )
    assert ret_id > 0

    # Strictly BEGIN IMMEDIATE and strictly ONE SQLite connection opened!
    assert tx_modes == ["IMMEDIATE"]
    assert len(opened_conns) == 1
    active_conn = opened_conns[0]

    for method_name, conns_seen in repo_conns.items():
        assert len(conns_seen) >= 1, f"Expected {method_name} to be called"
        for c in conns_seen:
            assert c is active_conn, f"{method_name} did not receive the shared transaction conn"


# ---------------------------------------------------------------------------
# 14. Controlled concurrency tests preventing concurrent over-returns
# ---------------------------------------------------------------------------

def test_concurrent_sale_returns_cannot_exceed_original_sold_quantity(env):
    """
    Sell 10 units on CREDIT, then launch 6 concurrent threads synchronized at a Barrier,
    each attempting to return 4 units simultaneously.
    Because BEGIN IMMEDIATE serializes validation + persistence, strictly 2 returns (8 units)
    must succeed and 4 must fail with ValueError; final DB state must match exactly.
    """
    db = env["db"]
    sales_svc = env["sales_svc"]
    inv_repo = env["inv_repo"]
    master_repo = env["master_repo"]
    acc_repo = env["acc_repo"]

    sale_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-CONC-01",
            sale_date="2026-11-17",
            customer_id=env["cust_id"],
            payment_mode="CREDIT",
            total_taxable=1000.0,
            total_cgst=90.0,
            total_sgst=90.0,
            net_amount=1180.0,
            paid_amount=0.0,
            due_amount=1180.0,
            items=[
                SaleItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=10.0,
                    sale_rate=118.0,
                    mrp=150.0,
                    taxable_amount=1000.0,
                    cgst_rate=9.0,
                    cgst_amount=90.0,
                    sgst_rate=9.0,
                    sgst_amount=90.0,
                    total_amount=1180.0,
                )
            ],
        )
    )
    assert inv_repo.get_batch_by_id(env["batch1_id"]).current_qty == pytest.approx(40.0)

    num_threads = 6
    barrier = threading.Barrier(num_threads)
    successes: List[int] = []
    failures: List[Exception] = []
    lock = threading.Lock()

    def _worker(idx: int):
        svc = SalesService(db)
        ret = SalesReturn(
            return_no=f"RET-CONC-{idx:02d}",
            return_date="2026-11-17",
            sale_id=sale_id,
            customer_id=env["cust_id"],
            total_taxable=400.0,
            total_tax=72.0,
            net_amount=472.0,
            items=[
                SalesReturnItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=4.0,
                    sale_rate=118.0,
                    taxable_amount=400.0,
                    tax_amount=72.0,
                    total_amount=472.0,
                )
            ],
        )
        barrier.wait()
        try:
            rid = svc.process_sale_return(ret)
            with lock:
                successes.append(rid)
        except Exception as exc:
            with lock:
                failures.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    # Strictly 2 successes (8 units <= 10 sold) and 4 rejections (exceeds eligible)
    assert len(successes) == 2
    assert len(failures) == 4
    assert all(isinstance(e, ValueError) and "exceeds eligible" in str(e) for e in failures)

    # Verify final DB state directly
    total_returned_qty = db.fetch_one(
        """
        SELECT COALESCE(SUM(sri.qty), 0) as total_qty
        FROM sales_return_items sri
        JOIN sales_returns sr ON sri.return_id = sr.return_id
        WHERE sr.sale_id = ?;
        """,
        (sale_id,),
    )["total_qty"]
    assert float(total_returned_qty) == pytest.approx(8.0)

    assert db.fetch_one("SELECT COUNT(*) as c FROM sales_returns WHERE sale_id = ?;", (sale_id,))["c"] == 2
    assert inv_repo.get_batch_by_id(env["batch1_id"]).current_qty == pytest.approx(48.0)
    assert db.fetch_one("SELECT COUNT(*) as c FROM stock_ledger WHERE transaction_type = 'SALE_RETURN';")["c"] == 2

    # Accounting & customer balance match: 1180 - 2 * 472 = 236.0
    assert master_repo.get_customer_by_id(env["cust_id"]).current_balance == pytest.approx(236.0)
    assert acc_repo.get_debtors_account().current_balance == pytest.approx(236.0)
    assert acc_repo.get_output_gst_account("CGST").current_balance == pytest.approx(18.0)
    assert acc_repo.get_output_gst_account("SGST").current_balance == pytest.approx(18.0)


def test_two_concurrent_full_returns_allow_strictly_one(env):
    """Two simultaneous full-quantity returns of the same invoice: strictly 1 succeeds, 1 fails."""
    db = env["db"]
    sales_svc = env["sales_svc"]
    inv_repo = env["inv_repo"]
    acc_repo = env["acc_repo"]

    sale_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-CONC-FULL",
            sale_date="2026-11-17",
            customer_id=env["cust_id"],
            payment_mode="CASH",
            total_taxable=500.0,
            total_cgst=45.0,
            total_sgst=45.0,
            net_amount=590.0,
            paid_amount=590.0,
            due_amount=0.0,
            items=[
                SaleItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=5.0,
                    sale_rate=118.0,
                    mrp=150.0,
                    taxable_amount=500.0,
                    cgst_rate=9.0,
                    cgst_amount=45.0,
                    sgst_rate=9.0,
                    sgst_amount=45.0,
                    total_amount=590.0,
                )
            ],
        )
    )

    barrier = threading.Barrier(2)
    successes: List[int] = []
    failures: List[Exception] = []
    lock = threading.Lock()

    def _worker(idx: int):
        svc = SalesService(db)
        ret = SalesReturn(
            return_no=f"RET-FULL-{idx}",
            return_date="2026-11-17",
            sale_id=sale_id,
            customer_id=env["cust_id"],
            total_taxable=500.0,
            total_tax=90.0,
            net_amount=590.0,
            items=[
                SalesReturnItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=5.0,
                    sale_rate=118.0,
                    taxable_amount=500.0,
                    tax_amount=90.0,
                    total_amount=590.0,
                )
            ],
        )
        barrier.wait()
        try:
            rid = svc.process_sale_return(ret)
            with lock:
                successes.append(rid)
        except Exception as exc:
            with lock:
                failures.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ValueError) and "exceeds eligible" in str(failures[0])
    assert inv_repo.get_batch_by_id(env["batch1_id"]).current_qty == pytest.approx(50.0)
    assert acc_repo.get_cash_account().current_balance == pytest.approx(0.0)
    assert acc_repo.get_output_gst_account("CGST").current_balance == pytest.approx(0.0)
    assert acc_repo.get_output_gst_account("SGST").current_balance == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 15. Phase 2B-2 Correction: Ambiguous Refund Rejection & Verified Channel Routing
# ---------------------------------------------------------------------------

def test_credit_sale_paid_in_full_later_rejects_ambiguous_refund_before_any_writes(env, monkeypatch):
    """
    A CREDIT sale paid in full via subsequent customer receipt has no invoice-allocated
    refund channel. Returning against it must fail BEFORE any stock/balance/return/voucher writes.
    """
    db = env["db"]
    sales_svc = env["sales_svc"]
    acc_svc = AccountingService(db)
    inv_repo = env["inv_repo"]
    master_repo = env["master_repo"]
    acc_repo = env["acc_repo"]

    sale_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-CRED-PAID-FULL",
            sale_date="2026-11-17",
            customer_id=env["cust_id"],
            payment_mode="CREDIT",
            total_taxable=500.0,
            total_cgst=45.0,
            total_sgst=45.0,
            net_amount=590.0,
            paid_amount=0.0,
            due_amount=590.0,
            items=[
                SaleItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=2.0,
                    sale_rate=295.0,
                    mrp=350.0,
                    taxable_amount=500.0,
                    cgst_rate=9.0,
                    cgst_amount=45.0,
                    sgst_rate=9.0,
                    sgst_amount=45.0,
                    total_amount=590.0,
                )
            ],
        )
    )
    # Customer pays off the credit balance in full via Bank Transfer
    acc_svc.record_customer_receipt(
        customer_id=env["cust_id"],
        receipt_date="2026-11-17",
        amount=590.0,
        payment_mode="BANK_TRANSFER",
    )
    assert master_repo.get_customer_by_id(env["cust_id"]).current_balance == pytest.approx(0.0)

    # Spy on every write method to prove rejection happens BEFORE any write is attempted
    write_calls = []
    for obj, name in (
        (sales_svc.inventory_repo, "add_batch_stock"),
        (sales_svc.inventory_repo, "record_stock_movement"),
        (sales_svc.sales_repo, "create_sale_return"),
        (sales_svc.master_repo, "update_customer_balance"),
        (sales_svc.accounting_repo, "create_voucher"),
    ):
        orig = getattr(obj, name)

        def _make_spy(fn, label):
            def _spy(*args, **kwargs):
                write_calls.append(label)
                return fn(*args, **kwargs)
            return _spy

        monkeypatch.setattr(obj, name, _make_spy(orig, name))

    vch_count_before = db.fetch_one("SELECT COUNT(*) as c FROM vouchers;")["c"]
    sl_count_before = db.fetch_one("SELECT COUNT(*) as c FROM stock_ledger;")["c"]
    cash_before = acc_repo.get_cash_account().current_balance
    bank_before = acc_repo.get_bank_account().current_balance

    with pytest.raises(ValueError, match="Refund method must be specified"):
        sales_svc.process_sale_return(
            SalesReturn(
                return_no="RET-CRED-PAID-FAIL",
                return_date="2026-11-17",
                sale_id=sale_id,
                customer_id=env["cust_id"],
                total_taxable=250.0,
                total_tax=45.0,
                net_amount=295.0,
                items=[
                    SalesReturnItem(
                        product_id=env["prod1_id"],
                        batch_id=env["batch1_id"],
                        qty=1.0,
                        sale_rate=295.0,
                        taxable_amount=250.0,
                        tax_amount=45.0,
                        total_amount=295.0,
                    )
                ],
            )
        )

    assert write_calls == []
    assert db.fetch_one("SELECT COUNT(*) as c FROM sales_returns;")["c"] == 0
    assert db.fetch_one("SELECT COUNT(*) as c FROM sales_return_items;")["c"] == 0
    assert db.fetch_one("SELECT COUNT(*) as c FROM vouchers;")["c"] == vch_count_before
    assert db.fetch_one("SELECT COUNT(*) as c FROM stock_ledger;")["c"] == sl_count_before
    assert inv_repo.get_batch_by_id(env["batch1_id"]).current_qty == pytest.approx(48.0)
    assert master_repo.get_customer_by_id(env["cust_id"]).current_balance == pytest.approx(0.0)
    assert acc_repo.get_cash_account().current_balance == pytest.approx(cash_before)
    assert acc_repo.get_bank_account().current_balance == pytest.approx(bank_before)


def test_partially_paid_credit_sale_return_exceeding_receivable_is_rejected_cleanly(env):
    """
    A CREDIT sale later partially paid via customer receipt (remaining due 295)
    must reject a 590 return because the excess 295 has no invoice-verified refund channel.
    """
    db = env["db"]
    sales_svc = env["sales_svc"]
    acc_svc = AccountingService(db)
    inv_repo = env["inv_repo"]
    master_repo = env["master_repo"]
    acc_repo = env["acc_repo"]

    sale_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-CRED-PART",
            sale_date="2026-11-17",
            customer_id=env["cust_id"],
            payment_mode="CREDIT",
            total_taxable=1000.0,
            total_cgst=90.0,
            total_sgst=90.0,
            net_amount=1180.0,
            paid_amount=0.0,
            due_amount=1180.0,
            items=[
                SaleItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=4.0,
                    sale_rate=295.0,
                    mrp=350.0,
                    taxable_amount=1000.0,
                    cgst_rate=9.0,
                    cgst_amount=90.0,
                    sgst_rate=9.0,
                    sgst_amount=90.0,
                    total_amount=1180.0,
                )
            ],
        )
    )
    # Customer pays 885 later -> remaining receivable is 295
    acc_svc.record_customer_receipt(
        customer_id=env["cust_id"],
        receipt_date="2026-11-17",
        amount=885.0,
        payment_mode="CASH",
    )
    assert master_repo.get_customer_by_id(env["cust_id"]).current_balance == pytest.approx(295.0)
    vch_count_before = db.fetch_one("SELECT COUNT(*) as c FROM vouchers;")["c"]

    # Return of 590 exceeds remaining receivable 295 -> excess 295 has ambiguous refund channel -> REJECT
    with pytest.raises(ValueError, match="Refund method must be specified"):
        sales_svc.process_sale_return(
            SalesReturn(
                return_no="RET-CRED-PART-FAIL",
                return_date="2026-11-17",
                sale_id=sale_id,
                customer_id=env["cust_id"],
                total_taxable=500.0,
                total_tax=90.0,
                net_amount=590.0,
                items=[
                    SalesReturnItem(
                        product_id=env["prod1_id"],
                        batch_id=env["batch1_id"],
                        qty=2.0,
                        sale_rate=295.0,
                        taxable_amount=500.0,
                        tax_amount=90.0,
                        total_amount=590.0,
                    )
                ],
            )
        )

    assert db.fetch_one("SELECT COUNT(*) as c FROM sales_returns;")["c"] == 0
    assert db.fetch_one("SELECT COUNT(*) as c FROM vouchers;")["c"] == vch_count_before
    assert inv_repo.get_batch_by_id(env["batch1_id"]).current_qty == pytest.approx(46.0)
    assert master_repo.get_customer_by_id(env["cust_id"]).current_balance == pytest.approx(295.0)
    assert acc_repo.get_debtors_account().current_balance == pytest.approx(295.0)
    assert acc_repo.get_cash_account().current_balance == pytest.approx(885.0)


def test_partially_paid_cash_sale_exceeding_invoice_recorded_cash_via_receipt_is_rejected(env):
    """
    Sale has 300 CASH at billing + 880 due. Customer later pays 680 via unallocated receipt (bal=200).
    A 590 return reduces 200 receivable and needs 390 refund, which exceeds the 300 invoice-recorded CASH.
    Must reject cleanly rather than assuming Cash for the unallocated receipt portion.
    """
    db = env["db"]
    sales_svc = env["sales_svc"]
    acc_svc = AccountingService(db)
    inv_repo = env["inv_repo"]
    master_repo = env["master_repo"]

    sale_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-CASH-CAP",
            sale_date="2026-11-17",
            customer_id=env["cust_id"],
            payment_mode="CASH",
            total_taxable=1000.0,
            total_cgst=90.0,
            total_sgst=90.0,
            net_amount=1180.0,
            paid_amount=300.0,
            due_amount=880.0,
            items=[
                SaleItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=4.0,
                    sale_rate=295.0,
                    mrp=350.0,
                    taxable_amount=1000.0,
                    cgst_rate=9.0,
                    cgst_amount=90.0,
                    sgst_rate=9.0,
                    sgst_amount=90.0,
                    total_amount=1180.0,
                )
            ],
        )
    )
    acc_svc.record_customer_receipt(
        customer_id=env["cust_id"],
        receipt_date="2026-11-17",
        amount=680.0,
        payment_mode="BANK_TRANSFER",
    )
    assert master_repo.get_customer_by_id(env["cust_id"]).current_balance == pytest.approx(200.0)

    with pytest.raises(ValueError, match="exceeds remaining invoice-recorded CASH payment"):
        sales_svc.process_sale_return(
            SalesReturn(
                return_no="RET-CASH-CAP-FAIL",
                return_date="2026-11-17",
                sale_id=sale_id,
                customer_id=env["cust_id"],
                total_taxable=500.0,
                total_tax=90.0,
                net_amount=590.0,
                items=[
                    SalesReturnItem(
                        product_id=env["prod1_id"],
                        batch_id=env["batch1_id"],
                        qty=2.0,
                        sale_rate=295.0,
                        taxable_amount=500.0,
                        tax_amount=90.0,
                        total_amount=590.0,
                    )
                ],
            )
        )

    assert db.fetch_one("SELECT COUNT(*) as c FROM sales_returns;")["c"] == 0
    assert inv_repo.get_batch_by_id(env["batch1_id"]).current_qty == pytest.approx(46.0)
    assert master_repo.get_customer_by_id(env["cust_id"]).current_balance == pytest.approx(200.0)


def test_split_payment_refund_is_rejected_while_receivable_only_portion_succeeds(env):
    """
    A SPLIT sale has no per-channel breakdown in the schema.
    - Full-paid SPLIT return (needs refund) MUST be rejected before any writes.
    - Partially paid SPLIT return within remaining due (refund=0) succeeds cleanly,
      but a subsequent return requiring refund (> remaining due) MUST be rejected.
    """
    db = env["db"]
    sales_svc = env["sales_svc"]
    inv_repo = env["inv_repo"]
    master_repo = env["master_repo"]
    acc_repo = env["acc_repo"]

    # Case A: Fully paid SPLIT sale
    sale_split_paid = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-SPLIT-PAID",
            sale_date="2026-11-17",
            customer_id=env["cust_id"],
            payment_mode="SPLIT",
            total_taxable=500.0,
            total_cgst=45.0,
            total_sgst=45.0,
            net_amount=590.0,
            paid_amount=590.0,
            due_amount=0.0,
            items=[
                SaleItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=2.0,
                    sale_rate=295.0,
                    mrp=350.0,
                    taxable_amount=500.0,
                    cgst_rate=9.0,
                    cgst_amount=45.0,
                    sgst_rate=9.0,
                    sgst_amount=45.0,
                    total_amount=590.0,
                )
            ],
        )
    )
    vch_before_a = db.fetch_one("SELECT COUNT(*) as c FROM vouchers;")["c"]

    with pytest.raises(ValueError, match="SPLIT payment without per-channel amounts"):
        sales_svc.process_sale_return(
            SalesReturn(
                return_no="RET-SPLIT-FAIL-A",
                return_date="2026-11-17",
                sale_id=sale_split_paid,
                customer_id=env["cust_id"],
                total_taxable=250.0,
                total_tax=45.0,
                net_amount=295.0,
                items=[
                    SalesReturnItem(
                        product_id=env["prod1_id"],
                        batch_id=env["batch1_id"],
                        qty=1.0,
                        sale_rate=295.0,
                        taxable_amount=250.0,
                        tax_amount=45.0,
                        total_amount=295.0,
                    )
                ],
            )
        )

    assert db.fetch_one("SELECT COUNT(*) as c FROM sales_returns;")["c"] == 0
    assert db.fetch_one("SELECT COUNT(*) as c FROM vouchers;")["c"] == vch_before_a
    assert inv_repo.get_batch_by_id(env["batch1_id"]).current_qty == pytest.approx(48.0)

    # Case B: Partially paid SPLIT sale (paid 600, due 580)
    sale_split_part = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-SPLIT-PART",
            sale_date="2026-11-17",
            customer_id=env["cust_id"],
            payment_mode="SPLIT",
            total_taxable=1000.0,
            total_cgst=90.0,
            total_sgst=90.0,
            net_amount=1180.0,
            paid_amount=600.0,
            due_amount=580.0,
            items=[
                SaleItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=4.0,
                    sale_rate=295.0,
                    mrp=350.0,
                    taxable_amount=1000.0,
                    cgst_rate=9.0,
                    cgst_amount=90.0,
                    sgst_rate=9.0,
                    sgst_amount=90.0,
                    total_amount=1180.0,
                )
            ],
        )
    )
    assert master_repo.get_customer_by_id(env["cust_id"]).current_balance == pytest.approx(580.0)

    # First return (295 <= 580 due): pure receivable reduction (refund_amount = 0) -> SUCCEEDS
    ret_ok = sales_svc.process_sale_return(
        SalesReturn(
            return_no="RET-SPLIT-OK-DEBTORS",
            return_date="2026-11-17",
            sale_id=sale_split_part,
            customer_id=env["cust_id"],
            total_taxable=250.0,
            total_tax=45.0,
            net_amount=295.0,
            items=[
                SalesReturnItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=1.0,
                    sale_rate=295.0,
                    taxable_amount=250.0,
                    tax_amount=45.0,
                    total_amount=295.0,
                )
            ],
        )
    )
    assert ret_ok > 0
    assert master_repo.get_customer_by_id(env["cust_id"]).current_balance == pytest.approx(285.0)
    assert acc_repo.get_debtors_account().current_balance == pytest.approx(285.0)
    stock_after_ok = inv_repo.get_batch_by_id(env["batch1_id"]).current_qty
    vch_after_ok = db.fetch_one("SELECT COUNT(*) as c FROM vouchers;")["c"]

    # Second return (590 > 285 remaining due): requires 305 refund on SPLIT -> REJECTED with zero writes!
    with pytest.raises(ValueError, match="SPLIT payment without per-channel amounts"):
        sales_svc.process_sale_return(
            SalesReturn(
                return_no="RET-SPLIT-FAIL-B",
                return_date="2026-11-17",
                sale_id=sale_split_part,
                customer_id=env["cust_id"],
                total_taxable=500.0,
                total_tax=90.0,
                net_amount=590.0,
                items=[
                    SalesReturnItem(
                        product_id=env["prod1_id"],
                        batch_id=env["batch1_id"],
                        qty=2.0,
                        sale_rate=295.0,
                        taxable_amount=500.0,
                        tax_amount=90.0,
                        total_amount=590.0,
                    )
                ],
            )
        )

    assert inv_repo.get_batch_by_id(env["batch1_id"]).current_qty == pytest.approx(stock_after_ok)
    assert db.fetch_one("SELECT COUNT(*) as c FROM vouchers;")["c"] == vch_after_ok
    assert master_repo.get_customer_by_id(env["cust_id"]).current_balance == pytest.approx(285.0)
    assert acc_repo.get_debtors_account().current_balance == pytest.approx(285.0)


def test_partially_paid_bank_transfer_sale_routes_verified_excess_to_bank_account(env):
    """
    When a partially paid BANK_TRANSFER sale records 700 BANK_TRANSFER + 480 due,
    a 590 return reliably reduces 480 Sundry Debtors and refunds the remaining 110 (<= 700) to Bank Account.
    """
    db = env["db"]
    sales_svc = env["sales_svc"]
    master_repo = env["master_repo"]
    acc_repo = env["acc_repo"]

    sale_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-BANK-PART",
            sale_date="2026-11-17",
            customer_id=env["cust_id"],
            payment_mode="BANK_TRANSFER",
            total_taxable=1000.0,
            total_cgst=90.0,
            total_sgst=90.0,
            net_amount=1180.0,
            paid_amount=700.0,
            due_amount=480.0,
            items=[
                SaleItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=4.0,
                    sale_rate=295.0,
                    mrp=350.0,
                    taxable_amount=1000.0,
                    cgst_rate=9.0,
                    cgst_amount=90.0,
                    sgst_rate=9.0,
                    sgst_amount=90.0,
                    total_amount=1180.0,
                )
            ],
        )
    )

    ret_id = sales_svc.process_sale_return(
        SalesReturn(
            return_no="RET-BANK-PART",
            return_date="2026-11-17",
            sale_id=sale_id,
            customer_id=env["cust_id"],
            total_taxable=500.0,
            total_tax=90.0,
            net_amount=590.0,
            items=[
                SalesReturnItem(
                    product_id=env["prod1_id"],
                    batch_id=env["batch1_id"],
                    qty=2.0,
                    sale_rate=295.0,
                    taxable_amount=500.0,
                    tax_amount=90.0,
                    total_amount=590.0,
                )
            ],
        )
    )

    entries = _get_return_voucher_entries_by_account(db, ret_id)
    assert entries == {
        "Sales Return Account": {"dr": 500.0, "cr": 0.0},
        "CGST Output Account": {"dr": 45.0, "cr": 0.0},
        "SGST Output Account": {"dr": 45.0, "cr": 0.0},
        "Sundry Debtors": {"dr": 0.0, "cr": 480.0},
        "Bank Account": {"dr": 0.0, "cr": 110.0},
    }
    assert master_repo.get_customer_by_id(env["cust_id"]).current_balance == pytest.approx(0.0)
    assert acc_repo.get_debtors_account().current_balance == pytest.approx(0.0)
    assert acc_repo.get_bank_account().current_balance == pytest.approx(590.0)
    assert acc_repo.get_cash_account().current_balance == pytest.approx(0.0)
