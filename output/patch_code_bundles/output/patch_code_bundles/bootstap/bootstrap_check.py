# bootstrap/bootstrap_check.py
# Version: 1.0
# Created: 2024-07-10
# Description: Verifies that critical runtime rows are present for safe system startup

import sqlite3
import sys
from datetime import datetime

DB_PATH = "/mnt/data/agents.db"

REQUIRED_LOCKS = [
    "patch_lock",
    "planner_lock",
    "goal_lock_agent_1",
]

def check_locks(conn):
    results = []
    for lock in REQUIRED_LOCKS:
        cur = conn.execute("SELECT 1 FROM lock_state WHERE lock_name = ?", (lock,))
        exists = cur.fetchone() is not None
        results.append({"lock": lock, "status": "ok" if exists else "missing"})
    return results

def check_exists(conn, table: str, key: str, value):
    try:
        cur = conn.execute(f"SELECT 1 FROM {table} WHERE {key} = ?", (value,))
        return cur.fetchone() is not None
    except sqlite3.OperationalError as e:
        print(f"❌ [error] Table/key check failed: {table}.{key} → {e}")
        return False

def main():
    print(f"🔍 Bootstrap Check — {datetime.utcnow().isoformat()}")

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        issues = []

        # Locks
        locks = check_locks(conn)
        for lock in locks:
            print(f"[lock] {lock['lock']}: {lock['status']}")
            if lock["status"] != "ok":
                issues.append(f"Missing lock: {lock['lock']}")

        # Agent ID 1
        if check_exists(conn, "agents", "id", 1):
            print("[agents] Agent ID 1: ok")
        else:
            print("❌ [agents] Agent ID 1 missing")
            issues.append("Missing Agent ID 1")

        # Goal ID 1
        if check_exists(conn, "goals", "id", 1):
            print("[goal] Goal ID 1: ok")
        else:
            print("❌ [goal] Goal ID 1 missing")
            issues.append("Missing Goal ID 1")

        # Patch Plan ID 1
        if check_exists(conn, "patch_plan", "id", 1):
            print("[patch_plan] Plan ID 1: ok")
        else:
            print("❌ [patch_plan] Plan ID 1 missing")
            issues.append("Missing Patch Plan ID 1")

        # Self-review for Agent ID 1
        if check_exists(conn, "agent_self_review", "agent_id", 1):
            print("[self_review] Agent 1 review exists: ok")
        else:
            print("❌ [self_review] No self-review for Agent 1")
            issues.append("Missing self-review for Agent 1")

        if issues:
            print("\n❌ Bootstrap validation failed:")
            for issue in issues:
                print(" -", issue)
            sys.exit(1)
        else:
            print("\n✅ All bootstrap checks passed.")

    except Exception as e:
        print(f"❌ Bootstrap check failed: {e}")
        sys.exit(1)

    finally:
        if 'conn' in locals():
            conn.close()

if __name__ == "__main__":
    main()

