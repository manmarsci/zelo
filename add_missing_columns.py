"""
One-time migration: add columns to the live MotherDuck 'products' table
that app.py already relies on (features, bundle_info, video_url) but
init_db.py's original schema never created.

Safe to run multiple times — each ALTER is wrapped so an already-added
column is skipped instead of raising an error.

Usage:
    python add_missing_columns.py
"""
import os
import duckdb
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("MOTHERDUCK_TOKEN")
DB = os.getenv("MOTHERDUCK_DATABASE", "zelo_boutique")
conn = duckdb.connect(f"md:{DB}?motherduck_token={TOKEN}")

# (column_name, DuckDB type + default)
COLUMNS_TO_ADD = [
    ("features", "VARCHAR DEFAULT '[]'"),      # JSON string of [{title, desc}, ...]
    ("bundle_info", "VARCHAR"),                # JSON string of {type, pieces, measurements}
    ("video_url", "VARCHAR"),                  # YouTube URL
]

existing_cols = {
    row[0] for row in conn.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'products'
    """).fetchall()
}

for col_name, col_def in COLUMNS_TO_ADD:
    if col_name in existing_cols:
        print(f"↷ Skipping '{col_name}' — already exists")
        continue
    try:
        conn.execute(f"ALTER TABLE products ADD COLUMN {col_name} {col_def};")
        print(f"✅ Added column '{col_name}'")
    except Exception as e:
        print(f"⚠️  Failed to add '{col_name}': {e}")

# Also used by /admin/customer_edit.html and /admin/smart-scan flows —
# checking here too since it's the same "schema drifted from init_db.py" issue.
existing_user_cols = {
    row[0] for row in conn.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'users'
    """).fetchall()
}
USER_COLUMNS_TO_ADD = [
    ("city", "VARCHAR"),
    ("address", "VARCHAR"),
    ("province", "VARCHAR"),
    ("postal_code", "VARCHAR"),
]
for col_name, col_def in USER_COLUMNS_TO_ADD:
    if col_name in existing_user_cols:
        print(f"↷ Skipping users.'{col_name}' — already exists")
        continue
    try:
        conn.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def};")
        print(f"✅ Added column users.'{col_name}'")
    except Exception as e:
        print(f"⚠️  Failed to add users.'{col_name}': {e}")

# courier_slips table is referenced in app.py (admin_customer_edit,
# admin_courier_update, admin_customer_add) but was never defined in
# init_db.py's SCHEMA at all — create it if missing.
conn.execute("""
    CREATE SEQUENCE IF NOT EXISTS courier_slips_id_seq START 1;
""")
conn.execute("""
    CREATE TABLE IF NOT EXISTS courier_slips (
        id INTEGER DEFAULT nextval('courier_slips_id_seq') PRIMARY KEY,
        customer_name VARCHAR,
        customer_phone VARCHAR,
        city VARCHAR,
        address VARCHAR,
        tracking_number VARCHAR UNIQUE,
        courier_name VARCHAR,
        description VARCHAR,
        charges VARCHAR,
        slip_date VARCHAR,
        created_at TIMESTAMP DEFAULT current_timestamp
    );
""")
print("✅ Ensured 'courier_slips' table exists")

# orders.updated_at is referenced by the /sitemap route added later in
# app.py (SELECT slug, updated_at FROM products WHERE is_active = 1)
# but products never got this column either.
if "updated_at" not in existing_cols:
    try:
        conn.execute("ALTER TABLE products ADD COLUMN updated_at TIMESTAMP DEFAULT current_timestamp;")
        print("✅ Added column 'updated_at' to products")
    except Exception as e:
        print(f"⚠️  Failed to add 'updated_at': {e}")

# payment_proof_url is used by upload_payment_proof()/payment_instructions()
# but is not in the orders schema in init_db.py.
existing_order_cols = {
    row[0] for row in conn.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'orders'
    """).fetchall()
}
if "payment_proof_url" not in existing_order_cols:
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN payment_proof_url VARCHAR;")
        print("✅ Added column 'payment_proof_url' to orders")
    except Exception as e:
        print(f"⚠️  Failed to add 'payment_proof_url': {e}")

conn.close()
print("\n🎉 Migration complete.")
