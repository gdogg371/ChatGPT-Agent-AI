from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping

# Fallback aliases so this reader does not depend on registry.canonicalize_family
_FALLBACK_ALIASES: Dict[str, str] = {
    # AST dotted/variants
    "ast.call": "ast_calls",
    "ast.calls": "ast_calls",
    "call": "ast_calls",
    "ast.symbol": "ast_symbols",
    "ast.symbols": "ast_symbols",
    "file": "ast_symbols",
    "class": "ast_symbols",
    "function": "ast_symbols",
    "ast.import": "ast_imports",
    "ast.imports": "ast_imports",
    "ast.xref": "ast_imports",
    "import": "ast_imports",
    "import_from": "ast_imports",
    "ast.import_from": "ast_imports",
    "from": "ast_imports",
    "edge.import": "ast_imports",
    "ast.docstring": "docs",

    # Scanner → canonical
    "js_ts": "js",
    "owners_index": "codeowners",
    "assets": "asset",
    "asset.index": "asset",
    "asset.file": "asset",
    "asset.summary": "asset",
    "git_info": "git",
    "license_scan": "license",
    "secrets_scan": "secrets",
    "env_index": "env",
    "deps_index": "deps",
    "deps.index": "deps",
    "deps.index.summary": "deps",     # <-- summary rows → deps
    "deps_index_summary": "deps",     # <-- underscored variant → deps
    "html_index": "html",
    "sql.index": "sql",
    "sql_index": "sql",

    # Docs/quality variants
    "docs.coverage": "docs",
    "doc_coverage": "docs",
    "quality.complexity": "quality",

    # IO/core
    "artifact": "io_core",
    "manifest": "io_core",
    "manifest.header": "io_core",
    "manifest.summary": "io_core",
    "module_index": "io_core",
}


class ManifestReader:
    """
    Streams rows from the chunked design manifest.
    Prefers the alias map from config (family_aliases), then falls back to _FALLBACK_ALIASES.
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
            k = str(k); v = str(v)
            self.alias_map[k] = v
            self.alias_map[k.replace(".", "_")] = v

        self.part_stem = str(self.transport.get("part_stem", "design_manifest"))
        self.part_ext = str(self.transport.get("part_ext", ".txt"))

    def _read_index_paths(self) -> List[Path]:
        if self.parts_index.exists():
            try:
                with self.parts_index.open("r", encoding="utf-8") as f:
                    idx = json.load(f)
                parts: List[Path] = []
                if isinstance(idx, dict):
                    if "parts" in idx and isinstance(idx["parts"], list):
                        for p in idx["parts"]:
                            if isinstance(p, str):
                                parts.append(self.manifest_dir / p)
                            elif isinstance(p, dict):
                                name = p.get("path") or p.get("name")
                                if name:
                                    parts.append(self.manifest_dir / str(name))
                    elif "files" in idx and isinstance(idx["files"], list):
                        for p in idx["files"]:
                            parts.append(self.manifest_dir / str(p))
                if parts:
                    return parts
            except Exception:
                pass
        return sorted(self.manifest_dir.glob(f"{self.part_stem}*{self.part_ext}"))

    def _canon(self, fam: str) -> str:
        if fam in self.alias_map:
            return self.alias_map[fam]
        u = fam.replace(".", "_")
        if u in self.alias_map:
            return self.alias_map[u]
        if fam in _FALLBACK_ALIASES:
            return _FALLBACK_ALIASES[fam]
        if u in _FALLBACK_ALIASES:
            return _FALLBACK_ALIASES[u]
        return u

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
                    fam = (
                        obj.get("family")
                        or obj.get("record_type")
                        or obj.get("kind")
                        or obj.get("type")
                        or ""
                    )
                    if not fam:
                        continue
                    obj["family"] = self._canon(str(fam))
                    yield obj


