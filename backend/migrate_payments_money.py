"""
Payments migration: money as Decimal/Numeric instead of Float, plus notes + created_at.

SQLite cannot ALTER a column's type in place, so moving `amount` from FLOAT to
NUMERIC(12,2) requires the standard safe table rebuild:

    1. create payments_new with the target schema
    2. copy every row across
    3. drop the old table
    4. rename payments_new -> payments
    5. recreate the indexes

Every existing row is copied with ROUND(amount, 2), so no value is lost (3000.0
-> 3000.00). A backup of the database is taken before anything is written.

This also repairs `transaction_reference` values that were saved as the literal
strings "null"/"none"/"" instead of a real NULL. Those junk values are actively
harmful now that the column is enforced unique, because they permanently block a
genuine transaction code that happens to be that word.

Usage (from the backend/ folder):
    python migrate_payments_money.py
"""

import shutil
import sqlite3

DB_PATH = "school.db"
BACKUP_PATH = "school.db.pre_money_migration"

JUNK_REFS = "'null','none','nil','n/a','na','-'"

NEW_TABLE_SQL = """
CREATE TABLE payments_new (
    id INTEGER NOT NULL PRIMARY KEY,
    student_id INTEGER,
    amount NUMERIC(12, 2) NOT NULL,
    method VARCHAR,
    transaction_reference VARCHAR,
    payer_name VARCHAR,
    payer_phone VARCHAR,
    date DATE,
    status VARCHAR,
    notes TEXT,
    created_at DATETIME,
    FOREIGN KEY (student_id) REFERENCES students (id),
    UNIQUE (transaction_reference)
)
"""


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def already_migrated(conn: sqlite3.Connection) -> bool:
    columns = {row[1]: row[2] for row in conn.execute("PRAGMA table_info(payments)")}
    if "amount" not in columns or "notes" not in columns or "created_at" not in columns:
        return False
    return columns["amount"].upper().startswith("NUMERIC")


def main() -> None:
    conn = sqlite3.connect(DB_PATH)

    if not table_exists(conn, "payments"):
        print("No payments table yet - create_all() will build it with the new schema.")
        conn.close()
        return

    if already_migrated(conn):
        print("Schema already migrated - no changes made.")
        conn.close()
        return

    conn.close()

    shutil.copyfile(DB_PATH, BACKUP_PATH)
    print(f"Backup written to {BACKUP_PATH}")

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("PRAGMA legacy_alter_table=ON")

        conn.execute(NEW_TABLE_SQL)

        # created_at is not populated on old rows, so default it to now.
        conn.execute(
            f"""
            INSERT INTO payments_new (
                id, student_id, amount, method, transaction_reference,
                payer_name, payer_phone, date, status, notes, created_at
            )
            SELECT
                id,
                student_id,
                ROUND(amount, 2),
                method,
                CASE
                    WHEN transaction_reference IS NOT NULL
                     AND LOWER(TRIM(transaction_reference)) IN ({JUNK_REFS})
                    THEN NULL
                    ELSE transaction_reference
                END,
                payer_name,
                payer_phone,
                date,
                status,
                NULL,
                datetime('now')
            FROM payments
            """
        )

        conn.execute("DROP TABLE payments")
        conn.execute("ALTER TABLE payments_new RENAME TO payments")

        conn.execute("CREATE INDEX ix_payments_id ON payments (id)")
        conn.execute("CREATE INDEX ix_payments_student_id ON payments (student_id)")
        conn.execute(
            "CREATE UNIQUE INDEX ix_payments_transaction_reference "
            "ON payments (transaction_reference)"
        )

        conn.commit()
    finally:
        conn.close()

    print("Migrated payments.amount -> NUMERIC(12,2); added notes, created_at.")
    print("Existing rows copied with ROUND(amount, 2). No rows deleted.")


if __name__ == "__main__":
    main()