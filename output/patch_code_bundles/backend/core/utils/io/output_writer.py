from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import json, csv
from typing import Dict, Any, List
from .file_ops import FileOps, FileOpsConfig

@dataclass
class OutputWriter:
    root: Path
    def __post_init__(self):
        self.fo = FileOps(FileOpsConfig())
        (self.root / "items").mkdir(parents=True, exist_ok=True)
        (self.root / "batches").mkdir(exist_ok=True)
        self._summary = self.root / "summary.csv"
        if not self._summary.exists():
            with self._summary.open("w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(["id","path","signature","outfile","ok","error"])

    def write_item(self, item: Dict[str, Any]) -> Path:
        p = self.root / "items" / f"{item['id']}.json"
        self.fo.write_text(p, json.dumps(item, ensure_ascii=False, indent=2)); return p

    def append_batch(self, items: List[Dict[str, Any]]) -> Path:
        p = self.root / "batches" / "batches.jsonl"
        with p.open("a", encoding="utf-8") as f:
            for it in items: f.write(json.dumps(it, ensure_ascii=False) + "\n")
        return p

    def append_summary(self, id: str, path: str, signature: str, outfile: str, ok: bool, error: str = "") -> None:
        with self._summary.open("a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([id, path, signature, outfile, "1" if ok else "0", error])
