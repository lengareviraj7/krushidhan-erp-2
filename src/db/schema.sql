-- ============================================================
-- Offline Agri-Input Shop ERP - Complete SQLite DDL Schema
-- Compatible with SQLite 3.35+
-- ============================================================

PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------
-- 1. MASTER DATA TABLES
-- ------------------------------------------------------------

-- Categories (Fertilizer, Seed, Pesticide, Hardware, Bio-Fertilizer, etc.)
CREATE TABLE IF NOT EXISTS categories (
    category_id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_name TEXT NOT NULL UNIQUE,
    short_name TEXT,
    is_hardware_category INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Manufacturers / Companies (IFFCO, Bayer, Syngenta, Mahyco, etc.)
CREATE TABLE IF NOT EXISTS manufacturers (
    manufacturer_id INTEGER PRIMARY KEY AUTOINCREMENT,
    manufacturer_name TEXT NOT NULL UNIQUE,
    contact_person TEXT,
    mobile TEXT,
    address TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Standard Measurement Units (Nos, KG, GM, LTR, ML, BAG, QTL, etc.)
CREATE TABLE IF NOT EXISTS units (
    unit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_name TEXT NOT NULL UNIQUE,
    symbol TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Unit Conversions (e.g. 1 BAG = 50 KG, 1 LTR = 1000 ML, 1 QTL = 100 KG)
CREATE TABLE IF NOT EXISTS unit_conversions (
    conversion_id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_unit_id INTEGER NOT NULL REFERENCES units(unit_id) ON DELETE CASCADE,
    to_unit_id INTEGER NOT NULL REFERENCES units(unit_id) ON DELETE CASCADE,
    conversion_factor REAL NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(from_unit_id, to_unit_id)
);

-- GST Tax Groups (Exempt, 5%, 12%, 18%, 28%)
CREATE TABLE IF NOT EXISTS tax_groups (
    tax_group_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tax_group_name TEXT NOT NULL UNIQUE,
    cgst_rate REAL DEFAULT 0,
    sgst_rate REAL DEFAULT 0,
    igst_rate REAL DEFAULT 0,
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Target Crops Master (Cotton, Soybean, Sugarcane, Wheat, Tomato, Onion, etc.)
CREATE TABLE IF NOT EXISTS crops (
    crop_id INTEGER PRIMARY KEY AUTOINCREMENT,
    crop_name TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Product Master (SKU)
CREATE TABLE IF NOT EXISTS products (
    product_id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_name TEXT NOT NULL,
    category_id INTEGER REFERENCES categories(category_id),
    manufacturer_id INTEGER REFERENCES manufacturers(manufacturer_id),
    hsn_code TEXT,
    unit_id INTEGER REFERENCES units(unit_id),
    tax_group_id INTEGER REFERENCES tax_groups(tax_group_id),
    default_purchase_rate REAL DEFAULT 0,
    default_sale_rate REAL DEFAULT 0,
    default_mrp REAL DEFAULT 0,
    min_stock_alert REAL DEFAULT 0,
    crop_id INTEGER REFERENCES crops(crop_id),
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Customer Groups / Categories (Retail, Wholesale, Farmer Club, etc.)
CREATE TABLE IF NOT EXISTS customer_groups (
    group_id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_name TEXT NOT NULL UNIQUE,
    discount_percent REAL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Customer Master (Farmers & Wholesale buyers)
CREATE TABLE IF NOT EXISTS customers (
    customer_id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_name TEXT NOT NULL,
    mobile TEXT,
    village TEXT,
    taluka TEXT,
    district TEXT,
    state TEXT DEFAULT 'Maharashtra',
    aadhar_no TEXT,
    gstin TEXT,
    group_id INTEGER REFERENCES customer_groups(group_id),
    opening_balance REAL DEFAULT 0,
    current_balance REAL DEFAULT 0,
    credit_limit REAL DEFAULT 0,
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Supplier / Distributor Master
CREATE TABLE IF NOT EXISTS suppliers (
    supplier_id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_name TEXT NOT NULL,
    contact_person TEXT,
    mobile TEXT,
    email TEXT,
    address TEXT,
    city TEXT,
    state TEXT DEFAULT 'Maharashtra',
    gstin TEXT,
    pan TEXT,
    dl_number TEXT,
    bank_name TEXT,
    account_no TEXT,
    ifsc_code TEXT,
    opening_balance REAL DEFAULT 0,
    current_balance REAL DEFAULT 0,
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Extra Charges Master (Freight, Loading/Unloading, Labour, Delivery)
CREATE TABLE IF NOT EXISTS extra_charges_master (
    charge_id INTEGER PRIMARY KEY AUTOINCREMENT,
    charge_name TEXT NOT NULL UNIQUE,
    tax_group_id INTEGER REFERENCES tax_groups(tax_group_id),
    default_amount REAL DEFAULT 0,
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);


-- ------------------------------------------------------------
-- 2. INVENTORY & BATCH MANAGEMENT TABLES
-- ------------------------------------------------------------

-- Batch-Wise Stock Management (FEFO support)
CREATE TABLE IF NOT EXISTS stock_batches (
    batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
    batch_no TEXT NOT NULL,
    mfg_date TEXT,
    exp_date TEXT,
    purchase_rate REAL NOT NULL DEFAULT 0,
    sale_rate REAL NOT NULL DEFAULT 0,
    mrp REAL NOT NULL DEFAULT 0,
    opening_qty REAL DEFAULT 0,
    current_qty REAL DEFAULT 0,
    barcode TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(product_id, batch_no)
);

-- Stock Ledger (Immutable stock audit trail for every IN/OUT event)
CREATE TABLE IF NOT EXISTS stock_ledger (
    ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(product_id),
    batch_id INTEGER REFERENCES stock_batches(batch_id),
    transaction_type TEXT NOT NULL, -- 'PURCHASE', 'PURCHASE_RETURN', 'SALE', 'SALE_RETURN', 'ADJUSTMENT'
    reference_type TEXT NOT NULL,   -- 'INVOICE', 'CHALLAN', 'MANUAL_ADJUSTMENT'
    reference_id INTEGER NOT NULL,
    movement_date TEXT DEFAULT CURRENT_TIMESTAMP,
    qty_in REAL DEFAULT 0,
    qty_out REAL DEFAULT 0,
    balance_qty REAL NOT NULL,
    rate REAL,
    remarks TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);


-- ------------------------------------------------------------
-- 3. PURCHASE MANAGEMENT TABLES
-- ------------------------------------------------------------

-- Inward Purchase Invoices
CREATE TABLE IF NOT EXISTS purchases (
    purchase_id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_no TEXT NOT NULL,
    purchase_date TEXT NOT NULL,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(supplier_id),
    challan_no TEXT,
    payment_type TEXT DEFAULT 'CREDIT', -- 'CASH', 'CREDIT', 'BANK_TRANSFER'
    total_taxable REAL DEFAULT 0,
    total_cgst REAL DEFAULT 0,
    total_sgst REAL DEFAULT 0,
    total_igst REAL DEFAULT 0,
    extra_charges REAL DEFAULT 0,
    round_off REAL DEFAULT 0,
    net_amount REAL NOT NULL,
    paid_amount REAL DEFAULT 0,
    due_amount REAL DEFAULT 0,
    remarks TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    idempotency_key TEXT,
    request_fingerprint TEXT
);

-- Purchase Items / Line Items
CREATE TABLE IF NOT EXISTS purchase_items (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_id INTEGER NOT NULL REFERENCES purchases(purchase_id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(product_id),
    batch_no TEXT NOT NULL,
    mfg_date TEXT,
    exp_date TEXT,
    qty REAL NOT NULL,
    free_qty REAL DEFAULT 0,
    unit_id INTEGER REFERENCES units(unit_id),
    purchase_rate REAL NOT NULL,
    sale_rate REAL NOT NULL,
    mrp REAL NOT NULL,
    discount_percent REAL DEFAULT 0,
    discount_amount REAL DEFAULT 0,
    taxable_amount REAL NOT NULL,
    cgst_rate REAL DEFAULT 0,
    cgst_amount REAL DEFAULT 0,
    sgst_rate REAL DEFAULT 0,
    sgst_amount REAL DEFAULT 0,
    igst_rate REAL DEFAULT 0,
    igst_amount REAL DEFAULT 0,
    total_amount REAL NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Purchase Return
CREATE TABLE IF NOT EXISTS purchase_returns (
    return_id INTEGER PRIMARY KEY AUTOINCREMENT,
    return_no TEXT NOT NULL UNIQUE,
    return_date TEXT NOT NULL,
    purchase_id INTEGER NOT NULL REFERENCES purchases(purchase_id) ON DELETE RESTRICT,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(supplier_id) ON DELETE RESTRICT,
    total_taxable REAL DEFAULT 0,
    total_cgst REAL CHECK (total_cgst IS NULL OR total_cgst >= 0),
    total_sgst REAL CHECK (total_sgst IS NULL OR total_sgst >= 0),
    total_igst REAL CHECK (total_igst IS NULL OR total_igst >= 0),
    total_tax REAL DEFAULT 0,
    round_off REAL DEFAULT 0,
    net_amount REAL NOT NULL,
    supplier_credit_note_no TEXT,
    supplier_credit_note_date TEXT,
    remarks TEXT,
    status TEXT CHECK (status IS NULL OR status IN ('POSTED', 'CANCELLED')),
    itc_status TEXT CHECK (itc_status IS NULL OR itc_status IN ('CLAIMED_REVERSED', 'NOT_CLAIMED', 'PENDING_CONFIRMATION', 'CONFIRMED_REVERSED')),
    voucher_id INTEGER UNIQUE REFERENCES vouchers(voucher_id) ON DELETE RESTRICT,
    itc_adjustment_voucher_id INTEGER UNIQUE REFERENCES vouchers(voucher_id) ON DELETE RESTRICT,
    reversal_voucher_id INTEGER UNIQUE REFERENCES vouchers(voucher_id) ON DELETE RESTRICT,
    itc_adjustment_reversal_voucher_id INTEGER UNIQUE REFERENCES vouchers(voucher_id) ON DELETE RESTRICT,
    cancelled_at TEXT,
    cancel_reason TEXT,
    idempotency_key TEXT UNIQUE,
    request_fingerprint TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Purchase Return Line Items
CREATE TABLE IF NOT EXISTS purchase_return_items (
    return_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    return_id INTEGER NOT NULL REFERENCES purchase_returns(return_id) ON DELETE RESTRICT,
    purchase_item_id INTEGER REFERENCES purchase_items(item_id) ON DELETE RESTRICT,
    product_id INTEGER NOT NULL REFERENCES products(product_id) ON DELETE RESTRICT,
    batch_id INTEGER REFERENCES stock_batches(batch_id) ON DELETE RESTRICT,
    qty REAL NOT NULL CHECK (qty > 0),
    purchase_rate REAL NOT NULL CHECK (purchase_rate >= 0),
    discount_amount REAL CHECK (discount_amount IS NULL OR discount_amount >= 0),
    net_rate REAL CHECK (net_rate IS NULL OR net_rate >= 0),
    taxable_amount REAL NOT NULL CHECK (taxable_amount >= 0),
    cgst_rate REAL CHECK (cgst_rate IS NULL OR cgst_rate >= 0),
    cgst_amount REAL CHECK (cgst_amount IS NULL OR cgst_amount >= 0),
    sgst_rate REAL CHECK (sgst_rate IS NULL OR sgst_rate >= 0),
    sgst_amount REAL CHECK (sgst_amount IS NULL OR sgst_amount >= 0),
    igst_rate REAL CHECK (igst_rate IS NULL OR igst_rate >= 0),
    igst_amount REAL CHECK (igst_amount IS NULL OR igst_amount >= 0),
    tax_amount REAL DEFAULT 0 CHECK (tax_amount >= 0),
    total_amount REAL NOT NULL CHECK (total_amount >= 0),
    reason TEXT
);

-- Supplier Credit Notes (Arising from Returns / Adjustments)
CREATE TABLE IF NOT EXISTS supplier_credits (
    credit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    credit_no TEXT NOT NULL UNIQUE,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(supplier_id) ON DELETE RESTRICT,
    return_id INTEGER NOT NULL REFERENCES purchase_returns(return_id) ON DELETE RESTRICT,
    credit_date TEXT NOT NULL,
    original_amount REAL NOT NULL CHECK (original_amount > 0),
    balance_amount REAL NOT NULL CHECK (balance_amount >= 0 AND balance_amount <= original_amount),
    status TEXT NOT NULL DEFAULT 'AVAILABLE' CHECK (status IN ('AVAILABLE', 'PARTIALLY_USED', 'FULLY_USED', 'CANCELLED')),
    notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_supplier_credits_return_id ON supplier_credits(return_id);
CREATE INDEX IF NOT EXISTS idx_supplier_credits_supplier ON supplier_credits(supplier_id);
CREATE INDEX IF NOT EXISTS idx_supplier_credits_status ON supplier_credits(status);

-- Purchase Return Multi-Settlement Allocations
CREATE TABLE IF NOT EXISTS purchase_return_settlements (
    settlement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    return_id INTEGER NOT NULL REFERENCES purchase_returns(return_id) ON DELETE RESTRICT,
    settlement_type TEXT NOT NULL CHECK (settlement_type IN ('PAYABLE_REDUCTION', 'REFUND_CASH', 'REFUND_BANK', 'SUPPLIER_CREDIT')),
    amount REAL NOT NULL CHECK (amount > 0),
    account_id INTEGER REFERENCES ledger_accounts(account_id) ON DELETE RESTRICT,
    credit_id INTEGER REFERENCES supplier_credits(credit_id) ON DELETE RESTRICT,
    reference_no TEXT,
    notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        (settlement_type = 'SUPPLIER_CREDIT' AND credit_id IS NOT NULL) OR
        (settlement_type != 'SUPPLIER_CREDIT' AND credit_id IS NULL)
    ),
    CHECK (
        (settlement_type IN ('REFUND_CASH', 'REFUND_BANK') AND account_id IS NOT NULL) OR
        (settlement_type NOT IN ('REFUND_CASH', 'REFUND_BANK'))
    )
);

CREATE INDEX IF NOT EXISTS idx_pr_settlements_return ON purchase_return_settlements(return_id);
CREATE INDEX IF NOT EXISTS idx_pr_settlements_credit ON purchase_return_settlements(credit_id);
CREATE INDEX IF NOT EXISTS idx_pr_settlements_account ON purchase_return_settlements(account_id);


CREATE INDEX IF NOT EXISTS idx_purchase_returns_purchase ON purchase_returns(purchase_id);
CREATE INDEX IF NOT EXISTS idx_purchase_returns_supplier ON purchase_returns(supplier_id);
CREATE INDEX IF NOT EXISTS idx_purchase_returns_date ON purchase_returns(return_date);

CREATE INDEX IF NOT EXISTS idx_pr_items_return ON purchase_return_items(return_id);
CREATE INDEX IF NOT EXISTS idx_pr_items_batch ON purchase_return_items(batch_id);
CREATE INDEX IF NOT EXISTS idx_pr_items_product ON purchase_return_items(product_id);

-- Immutability Triggers (Preventing Financial Hard Deletes)
CREATE TRIGGER IF NOT EXISTS trg_prevent_purchase_returns_delete
BEFORE DELETE ON purchase_returns
BEGIN
    SELECT RAISE(ABORT, 'Financial audit protection: Deletion of purchase returns is prohibited. Use cancellation reversal workflow.');
END;

CREATE TRIGGER IF NOT EXISTS trg_prevent_purchase_return_items_delete
BEFORE DELETE ON purchase_return_items
BEGIN
    SELECT RAISE(ABORT, 'Financial audit protection: Deletion of purchase return items is prohibited.');
END;

CREATE TRIGGER IF NOT EXISTS trg_prevent_purchase_return_settlements_delete
BEFORE DELETE ON purchase_return_settlements
BEGIN
    SELECT RAISE(ABORT, 'Financial audit protection: Deletion of purchase return settlements is prohibited.');
END;

CREATE TRIGGER IF NOT EXISTS trg_prevent_supplier_credits_delete
BEFORE DELETE ON supplier_credits
BEGIN
    SELECT RAISE(ABORT, 'Financial audit protection: Deletion of supplier credits is prohibited.');
END;


-- ------------------------------------------------------------
-- 4. SALES & BILLING TABLES
-- ------------------------------------------------------------

-- Sales Invoices
CREATE TABLE IF NOT EXISTS sales (
    sale_id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_no TEXT NOT NULL UNIQUE,
    sale_date TEXT NOT NULL,
    customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
    doctor_or_officer TEXT,
    payment_mode TEXT DEFAULT 'CASH', -- 'CASH', 'CREDIT', 'UPI', 'CARD', 'SPLIT'
    total_taxable REAL DEFAULT 0,
    total_cgst REAL DEFAULT 0,
    total_sgst REAL DEFAULT 0,
    total_igst REAL DEFAULT 0,
    total_discount REAL DEFAULT 0,
    extra_charges REAL DEFAULT 0,
    round_off REAL DEFAULT 0,
    net_amount REAL NOT NULL,
    paid_amount REAL DEFAULT 0,
    due_amount REAL DEFAULT 0,
    print_count INTEGER DEFAULT 0,
    remarks TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    idempotency_key TEXT,
    request_fingerprint TEXT
);

-- Sale Line Items
CREATE TABLE IF NOT EXISTS sale_items (
    sale_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id INTEGER NOT NULL REFERENCES sales(sale_id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(product_id),
    batch_id INTEGER NOT NULL REFERENCES stock_batches(batch_id),
    qty REAL NOT NULL,
    unit_id INTEGER REFERENCES units(unit_id),
    sale_rate REAL NOT NULL,
    mrp REAL NOT NULL,
    discount_percent REAL DEFAULT 0,
    discount_amount REAL DEFAULT 0,
    taxable_amount REAL NOT NULL,
    cgst_rate REAL DEFAULT 0,
    cgst_amount REAL DEFAULT 0,
    sgst_rate REAL DEFAULT 0,
    sgst_amount REAL DEFAULT 0,
    igst_rate REAL DEFAULT 0,
    igst_amount REAL DEFAULT 0,
    total_amount REAL NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Sales Returns
CREATE TABLE IF NOT EXISTS sales_returns (
    return_id INTEGER PRIMARY KEY AUTOINCREMENT,
    return_no TEXT NOT NULL UNIQUE,
    return_date TEXT NOT NULL,
    sale_id INTEGER REFERENCES sales(sale_id),
    customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
    total_taxable REAL DEFAULT 0,
    total_tax REAL DEFAULT 0,
    round_off REAL DEFAULT 0,
    net_amount REAL NOT NULL,
    remarks TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Sales Return Line Items
CREATE TABLE IF NOT EXISTS sales_return_items (
    return_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    return_id INTEGER NOT NULL REFERENCES sales_returns(return_id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(product_id),
    batch_id INTEGER REFERENCES stock_batches(batch_id),
    qty REAL NOT NULL,
    sale_rate REAL NOT NULL,
    taxable_amount REAL NOT NULL,
    tax_amount REAL DEFAULT 0,
    total_amount REAL NOT NULL,
    reason TEXT
);


-- ------------------------------------------------------------
-- 5. ACCOUNTS & FINANCIAL LEDGER TABLES
-- ------------------------------------------------------------

-- Chart of Accounts
CREATE TABLE IF NOT EXISTS ledger_accounts (
    account_id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_name TEXT NOT NULL UNIQUE,
    account_group TEXT NOT NULL, -- 'ASSETS', 'LIABILITIES', 'INCOME', 'EXPENSE', 'BANK_ACCOUNTS', 'CASH'
    opening_balance REAL DEFAULT 0,
    current_balance REAL DEFAULT 0,
    is_system INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Accounting Vouchers (Receipt, Payment, Contra, Journal, Debit Note, Credit Note)
CREATE TABLE IF NOT EXISTS vouchers (
    voucher_id INTEGER PRIMARY KEY AUTOINCREMENT,
    voucher_no TEXT NOT NULL UNIQUE,
    voucher_date TEXT NOT NULL,
    voucher_type TEXT NOT NULL, -- 'RECEIPT', 'PAYMENT', 'CONTRA', 'JOURNAL', 'DEBIT_NOTE', 'CREDIT_NOTE'
    total_amount REAL NOT NULL,
    narration TEXT,
    reference_type TEXT,        -- 'SALE', 'PURCHASE', 'EXPENSE', 'MANUAL'
    reference_id INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    idempotency_key TEXT,
    request_fingerprint TEXT
);

-- Double-Entry Ledger Posting Entries
CREATE TABLE IF NOT EXISTS ledger_entries (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    voucher_id INTEGER NOT NULL REFERENCES vouchers(voucher_id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES ledger_accounts(account_id),
    debit_amount REAL DEFAULT 0,
    credit_amount REAL DEFAULT 0,
    particulars TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Expense Categories (Electricity, Shop Rent, Staff Salary, Tea/Refreshment, Transport)
CREATE TABLE IF NOT EXISTS expense_categories (
    category_id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_name TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Expense Records
CREATE TABLE IF NOT EXISTS expenses (
    expense_id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL REFERENCES expense_categories(category_id),
    expense_date TEXT NOT NULL,
    amount REAL NOT NULL,
    payment_account_id INTEGER REFERENCES ledger_accounts(account_id),
    reference_no TEXT,
    remarks TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    idempotency_key TEXT,
    request_fingerprint TEXT
);


-- ------------------------------------------------------------
-- 6. SYSTEM, USERS & SECURITY TABLES
-- ------------------------------------------------------------

-- Application Users & Roles
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    full_name TEXT,
    role TEXT DEFAULT 'OPERATOR', -- 'ADMIN', 'MANAGER', 'OPERATOR'
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Shop / Enterprise Settings
CREATE TABLE IF NOT EXISTS company_settings (
    setting_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT NOT NULL,
    address TEXT,
    city TEXT,
    state TEXT DEFAULT 'Maharashtra',
    pincode TEXT,
    mobile TEXT,
    email TEXT,
    gstin TEXT,
    dl_fertilizer TEXT,
    dl_pesticide TEXT,
    dl_seed TEXT,
    bank_name TEXT,
    account_no TEXT,
    ifsc_code TEXT,
    invoice_terms TEXT,
    is_thermal_print INTEGER DEFAULT 0,
    default_invoice_size TEXT DEFAULT 'A4', -- 'A4', 'A5', '3INCH_THERMAL'
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Audit Trail
CREATE TABLE IF NOT EXISTS audit_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(user_id),
    action_type TEXT NOT NULL, -- 'INSERT', 'UPDATE', 'DELETE', 'LOGIN', 'BACKUP'
    table_name TEXT,
    record_id INTEGER,
    details TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Local Database Backups History
CREATE TABLE IF NOT EXISTS backup_log (
    backup_id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL,
    backup_type TEXT NOT NULL, -- 'MANUAL', 'SCHEDULED', 'BEFORE_UPGRADE'
    backup_size_bytes INTEGER,
    status TEXT DEFAULT 'SUCCESS',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Schema Version Tracking Table
CREATE TABLE IF NOT EXISTS schema_version (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_number INTEGER NOT NULL UNIQUE,
    description TEXT NOT NULL,
    applied_at TEXT DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO schema_version (version_id, version_number, description) VALUES (1, 1, 'Initial Commercial ERP Baseline Schema');

-- ------------------------------------------------------------
-- INDEXES FOR MAXIMUM QUERY PERFORMANCE & REPORTING
-- ------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_products_category ON products(category_id);
CREATE INDEX IF NOT EXISTS idx_products_hsn ON products(hsn_code);
CREATE INDEX IF NOT EXISTS idx_stock_batches_product ON stock_batches(product_id);
CREATE INDEX IF NOT EXISTS idx_stock_batches_exp ON stock_batches(exp_date);
CREATE INDEX IF NOT EXISTS idx_stock_ledger_product ON stock_ledger(product_id, batch_id);
CREATE INDEX IF NOT EXISTS idx_stock_ledger_date ON stock_ledger(movement_date);
CREATE INDEX IF NOT EXISTS idx_sales_date ON sales(sale_date);
CREATE INDEX IF NOT EXISTS idx_sales_customer ON sales(customer_id);
CREATE INDEX IF NOT EXISTS idx_sales_invoice_no ON sales(invoice_no);
CREATE INDEX IF NOT EXISTS idx_sale_items_sale ON sale_items(sale_id);
CREATE INDEX IF NOT EXISTS idx_sale_items_product ON sale_items(product_id);
CREATE INDEX IF NOT EXISTS idx_purchases_date ON purchases(purchase_date);
CREATE INDEX IF NOT EXISTS idx_purchases_supplier ON purchases(supplier_id);
CREATE INDEX IF NOT EXISTS idx_purchases_invoice_no ON purchases(invoice_no);
CREATE INDEX IF NOT EXISTS idx_purchase_items_purchase ON purchase_items(purchase_id);
CREATE INDEX IF NOT EXISTS idx_vouchers_date ON vouchers(voucher_date);
CREATE INDEX IF NOT EXISTS idx_ledger_entries_voucher ON ledger_entries(voucher_id);
CREATE INDEX IF NOT EXISTS idx_ledger_entries_account ON ledger_entries(account_id);
CREATE INDEX IF NOT EXISTS idx_customers_mobile ON customers(mobile);
CREATE INDEX IF NOT EXISTS idx_suppliers_mobile ON suppliers(mobile);

