from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping

from packager.emitters.registry import canonicalize_family as canon_fallback


class ManifestReader:
    """
    Streams rows from the chunked design manifest.

    It prefers the alias map supplied by loader (from config/packager.yml: family_aliases),
    then falls back to the registry's canonicalize rules.
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
        # Normalize keys in the supplied alias map so both dotted and underscored work
        self.alias_map: Dict[str, str] = {}
        for k, v in (family_aliases or {}).items():
            self.alias_map[str(k)] = str(v)
            self.alias_map[str(k).replace(".", "_")] = str(v)

        self.part_stem = str(self.transport.get("part_stem", "design_manifest"))
        self.part_ext = str(self.transport.get("part_ext", ".txt"))

    def _read_index_paths(self) -> List[Path]:
        if self.parts_index.exists():
            try:
                with self.parts_index.open("r", encoding="utf-8") as f:
                    idx = json.load(f)
                parts: List[Path] = []
                if isinstance(idx, dict):
                    # Common shapes:
                    # {"parts":[{"path":"design_manifest_0001.txt"}, ...]}
                    if "parts" in idx and isinstance(idx["parts"], list):
                        for p in idx["parts"]:
                            if isinstance(p, str):
                                parts.append(self.manifest_dir / p)
                            elif isinstance(p, dict):
                                # accept "path" or "name"
                                name = p.get("path") or p.get("name")
                                if name:
                                    parts.append(self.manifest_dir / str(name))
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

    def _canon(self, fam: str) -> str:
        if fam in self.alias_map:
            return self.alias_map[fam]
        u = fam.replace(".", "_")
        if u in self.alias_map:
            return self.alias_map[u]
        return canon_fallback(fam)

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
                    fam = obj.get("family") or obj.get("kind") or ""
                    if not fam:
                        continue
                    fam = str(fam)
                    obj["family"] = self._canon(fam)
                    yield obj
