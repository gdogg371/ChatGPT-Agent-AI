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
    Atomically write JSON to `path` with stable formatting:
      - UTF-8
      - sorted keys
      - trailing newline
    """
    ensure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, sort_keys=True, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def write_index(path: Path, index_data: Dict[str, Any]) -> None:
    """
    Convenience wrapper used by orchestrators to write an index JSON file.
    """
    write_json_atomic(path, index_data)


