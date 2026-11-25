#!/usr/bin/env python
"""
Migration script: Make pc_id nullable in client_accounts table
This script safely alters the client_accounts table to allow NULL pc_id values
without losing existing data.
"""

import sqlite3
import os
from datetime import datetime, timedelta, timezone

def migrate():
    db_path = os.path.join(os.path.dirname(__file__), 'instance', 'icms.db')

    if not os.path.exists(db_path):
        print(f"ERROR: Database not found at {db_path}")
        return False

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        # SQLite doesn't support ALTER COLUMN, so we recreate the table
        print("Starting migration: making pc_id nullable...")

        # Check if table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='client_accounts'")
        if not cursor.fetchone():
            print("INFO: Table 'client_accounts' does not exist. No migration needed.")
            conn.close()
            return True

        # Get the existing schema
        cursor.execute("PRAGMA table_info(client_accounts)")
        columns = cursor.fetchall()

        # Create new table with nullable pc_id
        create_new_table = """
        CREATE TABLE client_accounts_new (
            id INTEGER PRIMARY KEY,
            name VARCHAR(64) NOT NULL,
            pc_id INTEGER,
            password_hash VARCHAR(256) NOT NULL,
            active BOOLEAN DEFAULT 1,
            created_at DATETIME,
            FOREIGN KEY (pc_id) REFERENCES pcs (id)
        )
        """

        cursor.execute(create_new_table)

        # Copy existing data
        cursor.execute("""
        INSERT INTO client_accounts_new (id, name, pc_id, password_hash, active, created_at)
        SELECT id, name, pc_id, password_hash, active, created_at
        FROM client_accounts
        """)

        # Drop old table
        cursor.execute("DROP TABLE client_accounts")

        # Rename new table
        cursor.execute("ALTER TABLE client_accounts_new RENAME TO client_accounts")

        conn.commit()
        print("SUCCESS: Migration completed!")
        print("  - PC assignment is now optional (pc_id can be NULL)")
        print("  - All existing accounts preserved")
        return True

    except Exception as e:
        conn.rollback()
        print(f"ERROR: Migration failed: {e}")
        return False
    finally:
        conn.close()


if __name__ == "__main__":
    success = migrate()
    exit(0 if success else 1)
