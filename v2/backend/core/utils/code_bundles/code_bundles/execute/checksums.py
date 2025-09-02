# v2/backend/core/utils/code_bundles/code_bundles/execute/checksums.py

from __future__ import annotations
from hashlib import sha256
import sys
from pathlib import Path

# Ensure the embedded packager is importable first
ROOT = Path(__file__).parent
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

__all__ = ["_write_sha256sums_for_parts"]


def _write_sha256sums_for_parts(parts_dir: Path, sums_file: Path) -> None:
    parts_dir = Path(parts_dir)
    sums_file = Path(sums_file)
    with sums_file.open("w", encoding="utf-8", newline="\n") as f:
        for p in sorted(parts_dir.glob("**/*.txt")):
            digest = sha256(p.read_bytes()).hexdigest()
            rel = p.relative_to(parts_dir).as_posix()
            f.write(f"{digest}  {rel}\n")
