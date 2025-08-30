from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Tuple, Dict, Any
import json

from ..core.paths import PathOps
from ..core.integrity import Integrity


class BundleWriter:
    """Writes the JSONL bundle and a companion SHA256SUMS file."""

    def __init__(self, out_path: Path) -> None:
        self.out_path = out_path

    def write(self, records: Iterable[Dict[str, Any]]) -> None:
        """Write JSONL records to the manifest path."""
        PathOps.ensure_dir(self.out_path)
        with open(self.out_path, "w", encoding="utf-8") as f:
            for rec in records:
                json.dump(rec, f, ensure_ascii=False, sort_keys=True)
                f.write("\n")

    def write_sums(self, out_sums: Path, files: List[Tuple[str, bytes]]) -> None:
        """
        Write SHA256SUMS lines:
            <sha256>␠␠<relative-name>
        where the hash is computed over the file bytes provided.
        """
        lines = [f"{Integrity.sha256_bytes(data)}  {rel}" for (rel, data) in files]
        PathOps.ensure_dir(out_sums)
        with open(out_sums, "wb") as f:
            f.write(("\n".join(lines) + "\n").encode("utf-8"))

