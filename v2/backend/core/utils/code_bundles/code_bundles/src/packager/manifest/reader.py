from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional

from packager.emitters.registry import canonicalize_family


class ManifestReader:
    """
    Streams rows from the chunked design manifest.
    Tolerant to variations in parts index shape; falls back to a glob scan if needed.
    """

    def __init__(
        self,
        repo_root: Path,
        manifest_dir: Path,
        parts_index: Path,
        transport: Mapping[str, Any],
        family_aliases: Mapping[str, str],
    ) -> None:
        self.repo_root = repo_root
        self.manifest_dir = manifest_dir
        self.parts_index = parts_index
        self.transport = transport or {}
        self.family_aliases = dict(family_aliases)

        self.part_stem = str(self.transport.get("part_stem", "design_manifest"))
        self.part_ext = str(self.transport.get("part_ext", ".txt"))

    def _read_index_paths(self) -> List[Path]:
        if self.parts_index.exists():
            try:
                with self.parts_index.open("r", encoding="utf-8") as f:
                    idx = json.load(f)
                parts = []
                if isinstance(idx, dict):
                    # Common shapes:
                    # {"parts":[{"path":"design_manifest_0001.txt"}, ...]}
                    if "parts" in idx and isinstance(idx["parts"], list):
                        for p in idx["parts"]:
                            if isinstance(p, str):
                                parts.append(self.manifest_dir / p)
                            elif isinstance(p, dict) and "path" in p:
                                parts.append(self.manifest_dir / str(p["path"]))
                    # {"files":[...]} fallback
                    elif "files" in idx and isinstance(idx["files"], list):
                        for p in idx["files"]:
                            parts.append(self.manifest_dir / str(p))
                if parts:
                    return parts
            except Exception:
                # fall through to globbing
                pass

        # Fallback: glob by stem/ext under manifest_dir
        return sorted(self.manifest_dir.glob(f"{self.part_stem}*{self.part_ext}"))

    def iter_rows(self) -> Iterator[Dict[str, Any]]:
        part_paths = self._read_index_paths()
        for fp in part_paths:
            if not fp.exists():
                continue
            with fp.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    # Expect a family; normalize and yield
                    fam = obj.get("family") or obj.get("kind") or ""
                    fam = str(fam)
                    if not fam:
                        continue
                    obj["family"] = canonicalize_family(fam)
                    yield obj
