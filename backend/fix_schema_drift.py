"""
Non-destructive schema repair.

SQLAlchemy's Base.metadata.create_all() only CREATEs missing tables; it never
ALTERs existing ones. So when models.py gained new columns, the already-created
tables were left behind and queries started failing with
"no such column: ...".

This script repairs that drift using ALTER TABLE ... ADD COLUMN, which only ever
ADDS columns. No table is dropped, no row is deleted, no existing value is
rewritten. Running it twice is harmless (already-present columns are skipped).

Usage (from the backend/ folder):
    python fix_schema_drift.py
"""

import sqlite3

DB_PATH = "school.db"

# table -> [(column, sqlite_type, default_literal_or_None)]
# Keep this in sync with models.py. Additive only: never remove entries.
EXPECTED_COLUMNS = {
    "students": [
        ("parent_email", "VARCHAR", None),
        ("grade_level", "VARCHAR", None),
        ("student_tag", "VARCHAR", "'Day'"),
        ("status", "VARCHAR", "'Active'"),
        ("school_id", "INTEGER", None),
        ("photo_url", "VARCHAR", None),
    ],
    "payments": [
        ("school_id", "INTEGER", None),
    ],
    "payment_incoming": [
        ("school_id", "INTEGER", None),
    ],
    "audit_log": [
        ("school_id", "INTEGER", None),
    ],
    "document_staging": [
        ("school_id", "INTEGER", None),
    ],
    "student_accounts": [
        ("school_id", "INTEGER", None),
    ],
    "invoices": [
        ("school_id", "INTEGER", None),
    ],
    "notification_logs": [
        ("school_id", "INTEGER", None),
    ],
    "expenses": [
        ("school_id", "INTEGER", None),
    ],
}


def existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    added: list[str] = []

    for table, columns in EXPECTED_COLUMNS.items():
        if table not in {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }:
            print(f"  skip {table}: table does not exist yet (create_all will make it)")
            continue

        present = existing_columns(conn, table)
        for name, sql_type, default in columns:
            if name in present:
                continue
            default_clause = f" DEFAULT {default}" if default is not None else ""
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}{default_clause}")
            added.append(f"{table}.{name}")

    conn.commit()
    conn.close()

    if added:
        print("Added columns (existing data preserved):")
        for item in added:
            print(f"  + {item}")
    else:
        print("Schema already up to date - no changes made.")


if __name__ == "__main__":
    main()