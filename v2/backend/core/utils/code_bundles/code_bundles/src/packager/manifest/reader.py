"""
Robust reader for design-manifest parts that yields normalized manifest records.

Goals
-----
- Support JSON Lines *and* JSON array part files.
- Prefer a parts index file if present for deterministic ordering.
- Canonicalize family/kind/type names via an extensible alias map.
- Never raise on individual bad records; skip safely.

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
# Aliases (loaded from config/packager.yml → reader.aliases)
# ──────────────────────────────────────────────────────────────────────────────

# PyYAML is required to read config/packager.yml for alias configuration.
try:
    import yaml  # type: ignore
except Exception as e:  # pragma: no cover
    raise ImportError("PyYAML is required to read config/packager.yml for reader aliases.") from e


class ReaderConfigError(RuntimeError):
    """Configuration problem in config/packager.yml (reader.aliases)."""


def _resolve_cfg_path() -> Path:
    """
    Locate repo-root/config/packager.yml by walking upward from this file's directory.
    This is robust to callers running from arbitrary CWDs (e.g., execute/).
    """
    here = Path(__file__).resolve()
    for base in (here.parent, *here.parents):
        cand = base / "config" / "packager.yml"
        if cand.exists():
            return cand
    # As a last resort, check CWD (useful for direct runs at repo root)
    cwd_cand = Path("config") / "packager.yml"
    if cwd_cand.exists():
        return cwd_cand.resolve()
    raise ReaderConfigError(
        "Could not locate config/packager.yml by walking parents of reader.py or in the current working directory."
    )


def _load_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        raise ReaderConfigError(f"Failed to parse YAML at {path.resolve()}: {e}") from e
    if not isinstance(data, dict):
        raise ReaderConfigError(f"{path.resolve()} must parse to a mapping at top level.")
    return data


def _normalize_family_key(s: str) -> str:
    """Lowercase and normalize separators for alias lookup; do not over-normalize."""
    s = (s or "").strip()
    if not s:
        return ""
    s = s.replace("-", "_")
    return s.lower()


def _load_reader_aliases() -> Dict[str, str]:
    """
    Load reader.aliases from config/packager.yml and normalize keys/values.
    This replaces the old in-code _FALLBACK_ALIASES constant.
    """
    cfg_path = _resolve_cfg_path()
    root = _load_yaml(cfg_path)

    reader_block = root.get("reader")
    if not isinstance(reader_block, dict):
        raise ReaderConfigError("Missing required key 'reader' in config/packager.yml.")

    aliases_raw = reader_block.get("aliases")
    if not isinstance(aliases_raw, dict):
        raise ReaderConfigError("Missing required key 'reader.aliases' (mapping) in config/packager.yml.")

    aliases: Dict[str, str] = {}
    for k, v in aliases_raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            # skip non-string entries quietly; we only consume string→string
            continue
        aliases[_normalize_family_key(k)] = _normalize_family_key(v)
    return aliases


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
        Extra/override aliases for canonicalization; merged over YAML-provided map.
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

        # Base alias map from YAML, then overlay any caller-provided aliases.
        base_aliases = _load_reader_aliases()
        self.aliases: Dict[str, str] = dict(base_aliases)
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
