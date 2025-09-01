# File: v2/backend/core/utils/code_bundles/code_bundles/src/packager/manifest/reader.py
"""
Robust reader for design-manifest parts that yields normalized manifest records.

Goals
-----
- Support JSON Lines *and* JSON array part files.
- Prefer a parts index file if present for deterministic ordering.
- Canonicalize family/kind/type names via an extensible alias map.
- Never raise on individual bad records; skip safely.
- Stdlib-only.

Public API
----------
- ManifestReader(manifest_dir: Path, ...)
- ManifestReader.iter_manifest() -> Iterator[dict]
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional

__all__ = ["ManifestReader"]


# ──────────────────────────────────────────────────────────────────────────────
# Canonicalization (aliases)
# ──────────────────────────────────────────────────────────────────────────────

# NOTE: keys are lower-cased; incoming kinds are normalized to lower snake-ish.
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
    "method": "ast_symbols",

    "import": "ast_imports",
    "ast.import": "ast_imports",
    "ast.imports": "ast_imports",
    # Edge graph importer (what your run emits)
    "edge.import": "ast_imports",

    # Entrypoints
    "entrypoint": "entrypoints",
    "entrypoints": "entrypoints",
    "entrypoint.python": "entrypoints",
    "entrypoint.shell": "entrypoints",

    # JS
    "js": "js",
    "js.index": "js",

    # IO / manifest
    "io": "io_core",
    "manifest": "io_core",
    "manifest_header": "io_core",
    "bundle_summary": "io_core",

    # SBOM / deps
    "sbom": "sbom",
    "deps": "deps",
    "dep": "deps",
    "deps.index": "deps",
    "deps.index.summary": "deps",

    # Secrets
    "secret": "secrets",

    # Env
    "env": "env",
    "env.vars": "env",
    "env.usage": "env",

    # Quality / complexity
    "quality": "quality",
    "quality.metric": "quality",
    "quality_metrics": "quality",
    "quality.complexity": "quality",
    "quality_complexity": "quality",

    # SQL
    "sql": "sql",
    "sql.index": "sql",
    "sqlindex": "sql",

    # Ownership / licensing / html / git
    "codeowners": "codeowners",
    "license": "license",
    "html": "html",
    "git": "git",
    "git.info": "git",

    # Assets / catch-alls seen in manifests
    "asset": "asset",
    "asset.file": "asset",

    # Misc families present in some runs
    "cs": "cs",
    "docs.coverage": "docs.coverage",
    "docs.coverage.summary": "docs.coverage",
    "ast.xref": "ast.xref",
    "module_index": "module_index",
}


def _normalize_family_key(s: str) -> str:
    """Lowercase and normalize separators for alias lookup; do not over-normalize."""
    s = (s or "").strip()
    if not s:
        return ""
    s = s.replace("-", "_")
    return s.lower()


# ──────────────────────────────────────────────────────────────────────────────
# Reader
# ──────────────────────────────────────────────────────────────────────────────

class ManifestReader:
    """
    Reads a design-manifest directory and yields normalized objects (dicts)
    with a canonical 'family' key.

    Parameters
    ----------
    manifest_dir : Path | str
        Directory containing the design_manifest files (part files and index).
        Typically the *directory* that itself contains:
          - design_manifest_00_0001.txt
          - design_manifest_00_0002.txt
          - design_manifest_parts_index.json (optional)
    part_stem : str
        Filename prefix for parts (default: "design_manifest_").
    part_ext : str
        Filename extension for parts (default: ".txt").
    prefer_parts_index : bool
        When True, use the index file for deterministic ordering if present.
    aliases : Mapping[str, str] | None
        Extra/override aliases for canonicalization; merged over built-ins.
    """

    def __init__(
        self,
        manifest_dir: str | Path,
        *,
        part_stem: str = "design_manifest_",
        part_ext: str = ".txt",
        prefer_parts_index: bool = True,
        aliases: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.manifest_dir = Path(manifest_dir)
        self.part_stem = part_stem
        self.part_ext = part_ext
        self.prefer_parts_index = prefer_parts_index

        # Build alias map
        self.aliases: Dict[str, str] = dict(_FALLBACK_ALIASES)
        if aliases:
            for k, v in aliases.items():
                self.aliases[_normalize_family_key(k)] = _normalize_family_key(v)

    # ──────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────

    def iter_manifest(self) -> Iterator[Dict[str, Any]]:
        """
        Yield normalized manifest records (dicts) in deterministic order.

        Each yielded record is guaranteed to include a 'family' key with a
        canonicalized family name, derived from one of:
          - record['family'] | record['record_type'] | record['kind'] | record['type']
        """
        for part in self._resolve_part_files():
            yield from self._iter_part_file(part)

    # Back-compat alias (some older callers might use this name)
    def iter_items(self) -> Iterator[Dict[str, Any]]:
        return self.iter_manifest()

    # ──────────────────────────────────────────────────────────────────────
    # Internals
    # ──────────────────────────────────────────────────────────────────────

    def _resolve_part_files(self) -> List[Path]:
        """
        Resolve an ordered list of part files using parts index when available,
        falling back to a lexicographic glob over the standard stem/ext.
        """
        # 1) Try parts index
        if self.prefer_parts_index:
            idx = self.manifest_dir / "design_manifest_parts_index.json"
            if idx.exists():
                try:
                    data = json.loads(idx.read_text(encoding="utf-8"))
                    parts: List[Path] = []

                    # Accept several index shapes
                    if isinstance(data, dict):
                        seq = data.get("parts") or data.get("files") or []
                        if isinstance(seq, list):
                            for p in seq:
                                if isinstance(p, str):
                                    parts.append(self.manifest_dir / p)
                                elif isinstance(p, dict):
                                    name = p.get("path") or p.get("name")
                                    if name:
                                        parts.append(self.manifest_dir / str(name))
                    # Filter to existing files only, keep order
                    parts = [p for p in parts if p.exists()]
                    if parts:
                        return parts
                except Exception:
                    # Ignore index parsing issues; fall back to glob
                    pass

        # 2) Fallback: glob parts lexicographically
        glob = f"{self.part_stem}*{self.part_ext}"
        return sorted(self.manifest_dir.glob(glob))

    def _iter_part_file(self, path: Path) -> Iterator[Dict[str, Any]]:
        """
        Iterate a part file that may be JSONL or a JSON array.
        Invalid lines/objects are skipped safely.
        """
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
        except Exception:
            return

        if not text:
            return

        # Try JSON array first (fast path for array parts)
        if text.startswith("["):
            try:
                arr = json.loads(text)
                if isinstance(arr, list):
                    for obj in arr:
                        if isinstance(obj, dict):
                            norm = self._normalize_record(obj)
                            if norm is not None:
                                yield norm
                return
            except Exception:
                # fall through to JSONL parse
                pass

        # JSONL parse
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            norm = self._normalize_record(obj)
            if norm is not None:
                yield norm

    # Normalize and canonicalize a single record
    def _normalize_record(self, obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        fam = self._extract_family(obj)
        if not fam:
            # Keep unknowns only if caller wants all raw records; here we skip
            return None
        obj = dict(obj)  # shallow copy so we don't mutate caller buffers
        obj["family"] = fam
        return obj

    def _extract_family(self, obj: Mapping[str, Any]) -> str:
        """
        Extract and canonicalize a family name from a record.
        Checks common keys in priority order.
        """
        # Priority order: explicit family, record_type, kind, type
        raw = (
            obj.get("family")
            or obj.get("record_type")
            or obj.get("kind")
            or obj.get("type")
            or ""
        )
        fam = _normalize_family_key(str(raw))
        return self._canon(fam)

    def _canon(self, fam: str) -> str:
        """
        Canonicalize a normalized family name using alias map.
        Unrecognized families pass through unchanged (but normalized).
        """
        if not fam:
            return ""
        return self.aliases.get(fam, fam)




