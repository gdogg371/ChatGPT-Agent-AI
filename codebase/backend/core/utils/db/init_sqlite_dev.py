import os
import re
import sqlite3

BASE_DIR = "/databases\\"
DB_PATH = os.path.join(BASE_DIR, "bot_dev.db")
SCHEMA_DIR = "/scripts/sqlite_sql_schemas\\"

REQUIRED_TABLES = {
    "agent_insights": "003_create_agent_insights.sql",
    "diagnostics": "004_create_diagnostics.sql",
    "introspection_index": "043_create_introspection_index.sql",
}


def db_exists(path: str) -> bool:
    return os.path.isfile(path)


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?;", (table_name,)
    )
    return cursor.fetchone() is not None


def run_sql_file(conn: sqlite3.Connection, sql_path: str):
    with open(sql_path, "r", encoding="utf-8") as f:
        sql_script = f.read()

    # Strip manual BEGIN/COMMIT to avoid conflicts
    cleaned_sql = re.sub(r'\bBEGIN\s*;\s*', '', sql_script, flags=re.IGNORECASE)
    cleaned_sql = re.sub(r'\bCOMMIT\s*;\s*', '', cleaned_sql, flags=re.IGNORECASE)

    conn.executescript(cleaned_sql)
    print(f"✅ Executed: {os.path.basename(sql_path)}")



def init_database():
    db_created = False
    if not db_exists(DB_PATH):
        print(f"📂 Creating SQLite DB at {DB_PATH}")
        db_created = True
    else:
        print(f"📁 Using existing DB: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)

    for table, sql_file in REQUIRED_TABLES.items():
        if not table_exists(conn, table):
            sql_path = os.path.join(SCHEMA_DIR, sql_file)
            print(f"➕ Table '{table}' not found — applying: {sql_file}")
            run_sql_file(conn, sql_path)
        else:
            print(f"✔️ Table '{table}' already exists")

    # ❌ REMOVE THIS:
    # conn.commit()

    conn.close()

    if db_created:
        print("🎉 SQLite database initialized successfully.")
    else:
        print("🔄 SQLite database checked and up-to-date.")



if __name__ == "__main__":
    init_database()
