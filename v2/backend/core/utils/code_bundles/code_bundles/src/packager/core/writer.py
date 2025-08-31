from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict


def ensure_dir(path: Path) -> None:
    """
    Create the directory if it doesn't exist.
    """
    path.mkdir(parents=True, exist_ok=True)


def write_json_atomic(path: Path, data: Any) -> None:
    """
    Robust JSON writer:
      1) Write to a temporary sibling file
      2) Attempt atomic replace
      3) If replace fails (e.g., Windows/AV), fall back to direct write

    Always leaves a valid JSON file at 'path' or raises.
    """
    ensure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n"

    # Step 1: write temp
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        f.write(payload)

    # Step 2: try atomic replace
    try:
        os.replace(tmp, path)
        return
    except Exception:
        # Step 3: fallback — write directly (best effort)
        try:
            with path.open("w", encoding="utf-8", newline="\n") as f:
                f.write(payload)
        finally:
            # Clean up temp if still present
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass


def write_index(path: Path, index_data: Dict[str, Any]) -> None:
    """
    Convenience wrapper used by orchestrators to write an index JSON file.
    """
    write_json_atomic(path, index_data)



