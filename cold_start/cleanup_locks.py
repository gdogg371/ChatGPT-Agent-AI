# bootstrap/cleanup_locks.py
# Version: 1.0
# Created: 2024-07-18
# Description: Releases critical locks at shutdown to allow clean restart

from backend.core.locks.locks import release_lock
import sys
from datetime import datetime

LOCKS_TO_RELEASE = [
    "patch_lock",
    "planner_lock",
    "goal_lock_agent_1",
]

def cleanup_locks():
    print(f"🧹 Cleanup Start — {datetime.utcnow().isoformat()}")

    failures = []
    for lock in LOCKS_TO_RELEASE:
        try:
            release_lock(lock)
            print(f"[lock] Released: {lock}")
        except Exception as e:
            print(f"❌ Failed to release {lock}: {e}")
            failures.append(lock)

    if failures:
        print("\n⚠️ Some locks failed to release:")
        for lock in failures:
            print(f" - {lock}")
        sys.exit(1)
    else:
        print("✅ All locks successfully released.")

if __name__ == "__main__":
    cleanup_locks()

