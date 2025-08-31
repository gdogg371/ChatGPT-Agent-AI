# File: v2/backend/core/utils/code_bundles/code_bundles/src/packager/analysis_emitter.py
"""
Unified analysis sidecar emitter for the packager.

Goals
-----
- SINGLE SINK for all analysis outputs (existing + new) so everything looks the same.
- Reads chunked manifest parts (JSONL inside number-suffixed .txt) to backfill missing families.
- Normalizes naming/schema for analysis files using config.analysis_filenames.
- Writes:
    - analysis/*.summary.json (canonical family files)
    - analysis/_index.json (presence + sha + counts)
    - per-artifact *.header.json (for anything under emitted_prefix/**, excluding analysis/** and parts)
    - design_manifest/design_manifest.SHA256SUMS (GNU style)
    - compatibility alias: analysis/entrypoints.json (same content as entrypoints.summary.json)

Config it respects (from packager.yml)
--------------------------------------
- emitted_prefix (string)
- publish.publish_analysis (bool)
- publish.checksums (bool)
- transport.part_stem / transport.part_ext (for chunk discovery)
- metadata_emission (family → "none"|"manifest"|"both")
- analysis_filenames (family → canonical filename)
- family_aliases (legacy tokens → canonical family)
- controls.synthesize_empty_summaries (bool)
- controls.strict_validation (bool)
- controls.forbid_raw_secrets (bool)
- controls.analysis_strategy: "backfill" | "enforce" | "passthrough"  (optional, default: "backfill")

Public API
----------
    emit_all(repo_root: Path, cfg: Any) -> None

How to integrate (in run_pack.py)
---------------------------------
After your publish step (local/github) and once <emitted_prefix> is populated:

    from pathlib import Path
    from src.packager.analysis_emitter import emit_all as _emit_analysis_sidecars

    _emit_analysis_sidecars(repo_root=Path(cfg.source_root).resolve(), cfg=cfg)

"""

from __future__ import annotations

import json
import os
import re
import sys
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

# --------------------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------------------

def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _sha256_file(p: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()

def _read_json(p: Path) -> Any:
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)

def _write_text(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        f.write(text)

def _write_json(path: Path, data: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Avoid unnecessary rewrites (idempotent)
    new = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    if path.exists():
        try:
            old = path.read_text(encoding="utf-8")
            if old == new:
                return _sha256_file(path)
        except Exception:
            pass
    with path.open("w", encoding="utf-8") as f:
        f.write(new)
    return _sha256_file(path)

def _guess_content_type(p: Path) -> str:
    name = p.name.lower()
    if name.endswith(".json"):
        return "application/json"
    if name.endswith(".txt"):
        return "text/plain"
    if name.endswith(".zip"):
        return "application/zip"
    if name.endswith(".yml") or name.endswith(".yaml"):
        return "text/yaml"
    if name.endswith(".ndjson"):
        return "application/x-ndjson"
    return "application/octet-stream"

def _rel(p: Path, base: Path) -> str:
    try:
        return p.relative_to(base).as_posix()
    except Exception:
        return p.as_posix()

def _is_enveloped(obj: Any) -> bool:
    return (
        isinstance(obj, dict)
        and "items" in obj
        and "kind" in obj
        and "version" in obj
        and "generated_at_utc" in obj
        and "source" in obj
        and "stats" in obj
    )

# --------------------------------------------------------------------------------------
# Config adapter (supports dict or namespace; dotted-path access)
# --------------------------------------------------------------------------------------

class CfgAdapter:
    def __init__(self, cfg: Any):
        self._cfg = cfg

    def get(self, path: str, default: Any = None) -> Any:
        # dotted path getter handling dicts and namespaces
        cur: Any = self._cfg
        for token in path.split("."):
            if cur is None:
                return default
            if isinstance(cur, dict):
                cur = cur.get(token, None)
            else:
                cur = getattr(cur, token, None)
        return default if cur is None else cur

    def exists(self, path: str) -> bool:
        sentinel = object()
        return self.get(path, sentinel) is not sentinel

# --------------------------------------------------------------------------------------
# Raw chunked manifest indexer
# --------------------------------------------------------------------------------------

@dataclass
class RawRecord:
    file: str
    line_no: int
    obj: Dict[str, Any]

class RawManifestIndex:
    """
    Scans <emitted_prefix> for chunked manifest parts (design_manifest_*.txt),
    loads JSONL rows, and yields typed sections keyed by family/kind.
    """
    def __init__(self, part_dir: Path, part_stem: str = "design_manifest", part_ext: str = ".txt") -> None:
        self.part_dir = part_dir
        self.part_stem = part_stem
        self.part_ext = part_ext
        self.parts: List[Path] = []
        self.records: List[RawRecord] = []

    @classmethod
    def discover(cls, emitted_prefix: Path, part_stem: str, part_ext: str) -> "RawManifestIndex":
        candidates: List[Path] = []
        pat = re.compile(rf"{re.escape(part_stem)}_\d+_\d+{re.escape(part_ext)}$")
        # scan two levels deep for performance
        for base in [emitted_prefix, emitted_prefix / "design_manifest"]:
            if base.exists():
                for p in base.rglob(f"*{part_ext}"):
                    if pat.search(p.name):
                        candidates.append(p.parent)
        part_dir = max(candidates, key=lambda d: len(list(d.glob(f"{part_stem}_*{part_ext}")))) if candidates else emitted_prefix
        idx = cls(part_dir, part_stem, part_ext)
        idx._load()
        return idx

    def _load(self) -> None:
        self.parts = sorted(self.part_dir.glob(f"{self.part_stem}_*{self.part_ext}"))
        for part in self.parts:
            try:
                with part.open("r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, start=1):
                        s = line.strip()
                        if not s or s.startswith("#"):
                            continue
                        try:
                            obj = json.loads(s)
                            if isinstance(obj, dict):
                                self.records.append(RawRecord(file=part.name, line_no=i, obj=obj))
                        except Exception:
                            # Ignore corrupt lines
                            continue
            except Exception:
                continue

# --------------------------------------------------------------------------------------
# Analysis emitter
# --------------------------------------------------------------------------------------

class AnalysisEmitter:
    """
    The unified sink that:
      - Backfills missing analysis families from the raw chunks
      - Optionally normalizes existing analysis JSONs into a standard envelope
      - Writes _index.json, artifact headers, and SHA256SUMS
    """

    def __init__(self, repo_root: Path, cfg: Any):
        self.repo_root = repo_root.resolve()
        self.cfg = CfgAdapter(cfg)

        # Basic coordinates
        self.emitted_prefix = (self.repo_root / str(self.cfg.get("emitted_prefix", "output/patch_code_bundles"))).resolve()
        self.analysis_dir = self.emitted_prefix / "analysis"

        # Transport (chunks)
        self.part_stem = self.cfg.get("transport.part_stem", "design_manifest")
        self.part_ext = self.cfg.get("transport.part_ext", ".txt")

        # Families & filenames
        self.meta_emit: Dict[str, str] = self.cfg.get("metadata_emission", {}) or {}
        self.analysis_filenames: Dict[str, str] = self.cfg.get("analysis_filenames", {}) or {}
        self.family_aliases: Dict[str, str] = self.cfg.get("family_aliases", {}) or {}

        # Controls
        self.strategy: str = str(self.cfg.get("controls.analysis_strategy", "backfill")).lower()
        if self.strategy not in {"backfill", "enforce", "passthrough"}:
            self.strategy = "backfill"
        self.synthesize_empty: bool = bool(self.cfg.get("controls.synthesize_empty_summaries", True))
        self.strict_validation: bool = bool(self.cfg.get("controls.strict_validation", True))
        self.forbid_raw_secrets: bool = bool(self.cfg.get("controls.forbid_raw_secrets", True))

        # Publish toggles
        self.publish_analysis: bool = bool(self.cfg.get("publish_analysis", True))
        self.emit_checksums: bool = bool(self.cfg.get("publish.checksums", False))

        # Manifest directory for sums
        cand = self.emitted_prefix / "design_manifest"
        self.manifest_dir = cand if cand.exists() else self.emitted_prefix

    # ---------------- Public entrypoint ----------------

    def run(self) -> None:
        if not self.publish_analysis:
            self._log("[analysis] publish_analysis=false → skip")
            return

        # Discover raw chunked JSONL
        raw_idx = RawManifestIndex.discover(self.emitted_prefix, self.part_stem, self.part_ext)

        # Discover existing analysis files
        existing = self._discover_existing_analysis()

        # Resolve canonical families (those configured as "both")
        target_families = {fam for fam, mode in self.meta_emit.items() if (mode or "").lower() == "both"}

        # 1) Normalize/emit according to strategy
        written: Dict[str, Tuple[Path, str, int]] = {}  # fam -> (path, sha, count)

        if self.strategy == "passthrough":
            # Just index and emit headers/checksums later
            pass

        elif self.strategy == "backfill":
            # Keep existing files as-is, emit missing canonical ones
            # (and optionally synthesize empties)
            # First, include existing canonical files into the "written" map
            for fam in target_families:
                canon = self._canonical_path_for_family(fam)
                if canon and canon.exists():
                    obj = _read_json(canon)
                    count = self._count_items(obj)
                    sha = _write_json(canon, obj)  # idempotent (ensures trailing newline)
                    written[fam] = (canon, sha, count)

            # Backfill missing canonical files from raw or synthesize
            raw_groups = self._group_raw_by_family(raw_idx)
            for fam in sorted(target_families):
                if fam in written:
                    continue
                items = raw_groups.get(fam, [])
                if fam == "secrets" and items:
                    items = [self._redact_secret(x) for x in items]
                if items or self.synthesize_empty:
                    doc = self._envelope(fam, items)
                    out_path = self._canonical_path_for_family(fam, create=True)
                    sha = _write_json(out_path, doc)
                    written[fam] = (out_path, sha, len(items))
                    # Compatibility alias for entrypoints
                    if fam == "entrypoints":
                        _write_json(self.analysis_dir / "entrypoints.json", doc)

            # If legacy entrypoints.json exists but canonical is still missing, mirror it
            if "entrypoints" in target_families and "entrypoints" not in written:
                legacy = self.analysis_dir / "entrypoints.json"
                if legacy.exists():
                    data = _read_json(legacy)
                    if not _is_enveloped(data):
                        data = self._envelope("entrypoints", self._coerce_items(data))
                    out_path = self._canonical_path_for_family("entrypoints", create=True)
                    sha = _write_json(out_path, data)
                    written["entrypoints"] = (out_path, sha, self._count_items(data))
                    _write_json(legacy, data)

        elif self.strategy == "enforce":
            # Rewrite everything into canonical naming/envelope
            raw_groups = self._group_raw_by_family(raw_idx)
            # Combine families: all targets + any family present in existing analysis folder
            present_families = set(target_families) | set(existing.keys())
            for fam in sorted(present_families):
                # preferred source priority:
                # 1) existing analysis content (any name), 2) raw groups, 3) synthesize []
                data = self._choose_best_source_for_family(fam, existing, raw_groups)
                # Envelope (and secrets redaction)
                items = self._coerce_items(data)
                if fam == "secrets" and items:
                    items = [self._redact_secret(x) for x in items]
                doc = self._envelope(fam, items)
                # write canonical
                out_path = self._canonical_path_for_family(fam, create=True)
                sha = _write_json(out_path, doc)
                written[fam] = (out_path, sha, len(items))
                # write compatibility alias if needed
                if fam == "entrypoints":
                    _write_json(self.analysis_dir / "entrypoints.json", doc)

        # 2) Always write analysis/_index.json (existing + written)
        index = self._build_index(existing_paths=existing, newly_written=written)
        _write_json(self.analysis_dir / "_index.json", index)

        # 3) Emit per-artifact headers (standardize)
        self._emit_artifact_headers()

        # 4) Emit checksums if configured
        if self.emit_checksums:
            self._emit_checksums()

        # Summary log
        fam_counts = ", ".join(f"{k}:{v[2]}" for k, v in sorted(written.items()))
        self._log(f"[analysis] strategy={self.strategy} wrote {len(written)} families ({fam_counts}); "
                  f"index + headers{' + checksums' if self.emit_checksums else ''}")

    # ---------------- Helpers: discovery & grouping ----------------

    def _discover_existing_analysis(self) -> Dict[str, List[Tuple[Path, Any]]]:
        """
        Returns: {family: [(path, json_obj), ...]}
        Uses analysis_filenames and family_aliases to map name → family.
        """
        out: Dict[str, List[Tuple[Path, Any]]] = {}

        if not self.analysis_dir.exists():
            return out

        # Build inverse filename→family map
        inv_map: Dict[str, str] = {}
        for fam, fname in self.analysis_filenames.items():
            inv_map[fname] = fam

        for p in self.analysis_dir.glob("*.json"):
            name = p.name
            try:
                obj = _read_json(p)
            except Exception:
                continue

            fam = None
            # 1) direct filename mapping
            if name in inv_map:
                fam = inv_map[name]
            else:
                # 2) heuristic: strip suffixes
                fam = self._family_from_filename(name)
            if fam:
                fam = self._normalize_family(fam)
                out.setdefault(fam, []).append((p, obj))

        return out

    def _group_raw_by_family(self, raw_idx: RawManifestIndex) -> Dict[str, List[Dict[str, Any]]]:
        out: Dict[str, List[Dict[str, Any]]] = {}
        for rr in raw_idx.records:
            obj = rr.obj
            fam = self._family_from_raw(obj)
            if not fam:
                continue
            fam = self._normalize_family(fam)
            if fam == "secrets" and self.forbid_raw_secrets:
                obj = self._redact_secret(obj)
            out.setdefault(fam, []).append(obj)
        return out

    # ---------------- Helpers: family naming & envelopes ----------------

    def _normalize_family(self, token: str) -> str:
        t = (token or "").strip()
        if not t:
            return t
        t = t.replace("-", "_")
        # First alias map exact, then with dots->underscores
        fam = self.family_aliases.get(t, t)
        if fam == t and "." in t:
            fam = self.family_aliases.get(t.replace(".", "_"), fam)
        return fam

    def _family_from_raw(self, obj: Dict[str, Any]) -> Optional[str]:
        # Prefer "kind" like "asset.summary" → "asset"
        kind = str(obj.get("kind") or "")
        if kind:
            return kind.split(".", 1)[0]
        # Else use "family" if present
        fam = obj.get("family")
        return str(fam) if fam else None

    def _family_from_filename(self, filename: str) -> Optional[str]:
        # Exact: entrypoints.json → entrypoints
        if filename == "entrypoints.json":
            return "entrypoints"
        # Try "*.summary.json" → piece before first dot
        m = re.match(r"^([a-zA-Z0-9_]+)\.summary\.json$", filename)
        if m:
            return m.group(1)
        # Special cases from config
        if filename.endswith(".cyclonedx.json"):
            return "sbom"
        if filename.endswith(".index.summary.json"):
            # e.g., deps.index.summary.json => 'deps'
            return filename.split(".")[0]
        if filename.endswith(".info.summary.json"):
            # git.info.summary.json => 'git'
            return filename.split(".")[0]
        if filename.endswith(".calls.summary.json"):
            return "ast_calls"
        if filename.endswith(".imports.summary.json"):
            return "ast_imports"
        if filename.endswith(".symbols.summary.json"):
            return "ast_symbols"
        return None

    def _canonical_path_for_family(self, fam: str, create: bool = False) -> Path:
        fname = self.analysis_filenames.get(fam, f"{fam}.summary.json")
        p = self.analysis_dir / fname
        if create:
            p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def _common_source_header(self) -> Dict[str, Any]:
        return {
            "packager_version": self.cfg.get("version", None),
            "config_path": self.cfg.get("_config_path", None),
            "git": {
                "branch": self.cfg.get("git_branch", self.cfg.get("publish.github.branch", None)),
                "commit": self.cfg.get("git_commit", None),
                "dirty": self.cfg.get("git_dirty", None),
                "remote_owner": self.cfg.get("publish.github.owner", None),
                "remote_repo": self.cfg.get("publish.github.repo", None),
            },
        }

    def _envelope(self, fam: str, items: List[Any]) -> Dict[str, Any]:
        return {
            "kind": f"design_manifest.analysis/{fam}",
            "version": "1.0",
            "generated_at_utc": _utcnow(),
            "source": self._common_source_header(),
            "stats": {"count": len(items)},
            "items": items,
        }

    def _coerce_items(self, data: Any) -> List[Any]:
        if _is_enveloped(data):
            it = data.get("items")
            return list(it) if isinstance(it, list) else []
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "items" in data and isinstance(data["items"], list):
            return data["items"]
        # otherwise wrap single dict as list
        return [data] if isinstance(data, dict) else []

    def _count_items(self, data: Any) -> int:
        if _is_enveloped(data):
            it = data.get("items")
            return len(it) if isinstance(it, list) else 0
        if isinstance(data, list):
            return len(data)
        if isinstance(data, dict) and "items" in data and isinstance(data["items"], list):
            return len(data["items"])
        return 1 if isinstance(data, dict) else 0

    def _choose_best_source_for_family(
        self,
        fam: str,
        existing: Dict[str, List[Tuple[Path, Any]]],
        raw_groups: Dict[str, List[Dict[str, Any]]],
    ) -> Any:
        # 1) Prefer the canonical named file if present
        canon = self._canonical_path_for_family(fam)
        if canon.exists():
            try:
                return _read_json(canon)
            except Exception:
                pass
        # 2) Any existing file for that family
        if fam in existing and existing[fam]:
            # If multiple, prefer enveloped one
            enveloped = [obj for (_p, obj) in existing[fam] if _is_enveloped(obj)]
            if enveloped:
                return enveloped[0]
            return existing[fam][0][1]
        # 3) Raw
        if fam in raw_groups:
            return self._envelope(fam, raw_groups[fam])
        # 4) Synthesize empty if allowed
        if self.synthesize_empty:
            return self._envelope(fam, [])
        return []

    # ---------------- Helpers: secrets, headers, sums, index ----------------

    def _redact_secret(self, obj: Dict[str, Any]) -> Dict[str, Any]:
        # Aggressive removal of sensitive values
        redact_keys = {
            "value", "secret", "token", "password", "access_key", "private_key",
            "match", "credential", "apikey", "api_key", "client_secret"
        }
        safe: Dict[str, Any] = {}
        for k, v in obj.items():
            if k in redact_keys:
                continue
            if k == "exemplar" and isinstance(v, str):
                safe[k] = (v[:2] + "…" + v[-2:]) if len(v) > 8 else "…"
            else:
                safe[k] = v
        # Also ensure payloads under nested structures are not leaking obvious long tokens
        for k, v in list(safe.items()):
            if isinstance(v, str) and len(v) >= 24 and re.search(r"[A-Za-z0-9_\-]{24,}", v):
                safe[k] = v[:2] + "…" + v[-2:]
        return safe

    def _emit_artifact_headers(self) -> None:
        """
        Emit <file>.header.json for all files under emitted_prefix/* excluding:
          - analysis/**
          - chunked parts (design_manifest_XX_YYYY.txt)
          - the checksum file itself
          - existing *.header.json files
        """
        part_pat = re.compile(rf"{re.escape(self.part_stem)}_\d+_\d+{re.escape(self.part_ext)}$")
        for p in self.emitted_prefix.rglob("*"):
            if not p.is_file():
                continue
            rel = _rel(p, self.emitted_prefix)
            if rel.startswith("analysis/"):
                continue
            if p.name.endswith(".header.json"):
                continue
            if p.name == "design_manifest.SHA256SUMS":
                continue
            if part_pat.match(p.name):
                continue

            header = {
                "path": rel,
                "size": p.stat().st_size,
                "sha256": _sha256_file(p),
                "created_utc": _utcnow(),
                "content_type": _guess_content_type(p),
            }
            _write_json(p.with_name(p.name + ".header.json"), header)

    def _emit_checksums(self) -> None:
        """
        Write GNU-style SHA256 sums under:
            <emitted_prefix>/design_manifest/design_manifest.SHA256SUMS
        Mirror to <repo_root>/output/design_manifest/ if that folder exists (legacy).
        """
        primary_dir = self.manifest_dir if self.manifest_dir.exists() else self.emitted_prefix
        lines: List[str] = []
        for p in sorted(self.emitted_prefix.rglob("*")):
            if not p.is_file():
                continue
            rel = _rel(p, self.emitted_prefix)
            if p.name == "design_manifest.SHA256SUMS":
                continue
            digest = _sha256_file(p)
            lines.append(f"{digest} *{rel}")
        sums_path = primary_dir / "design_manifest.SHA256SUMS"
        sums_path.parent.mkdir(parents=True, exist_ok=True)
        _write_text(sums_path, "\n".join(lines) + "\n")

        legacy = self.repo_root / "output" / "design_manifest"
        if legacy.exists():
            legacy.mkdir(parents=True, exist_ok=True)
            _write_text(legacy / "design_manifest.SHA256SUMS", "\n".join(lines) + "\n")

        self._log(f"[analysis] wrote {_rel(sums_path, self.repo_root)}")

    def _build_index(
        self,
        existing_paths: Dict[str, List[Tuple[Path, Any]]],
        newly_written: Dict[str, Tuple[Path, str, int]],
    ) -> Dict[str, Any]:
        """
        Build analysis/_index.json including all families present after this run.
        existing_paths: discovered before writing (may include legacy names)
        newly_written: canonical family files written in this run
        """
        header = {
            "kind": "design_manifest.analysis/index",
            "version": "1.0",
            "generated_at_utc": _utcnow(),
        }

        # Aggregate canonical entries
        families: Dict[str, Dict[str, Any]] = {}

        # First, ingest newly written canonical files
        for fam, (path, sha, count) in newly_written.items():
            families[fam] = {
                "path": _rel(path, self.emitted_prefix),
                "sha256": sha,
                "count": count,
            }

        # Then, add existing canonical files not rewritten this run
        for fam, entries in existing_paths.items():
            canon = self._canonical_path_for_family(fam)
            if fam not in families and canon.exists():
                try:
                    obj = _read_json(canon)
                    count = self._count_items(obj)
                    sha = _sha256_file(canon)
                    families[fam] = {
                        "path": _rel(canon, self.emitted_prefix),
                        "sha256": sha,
                        "count": count,
                    }
                except Exception:
                    continue

        # Include a compatibility note for entrypoints legacy alias if present
        compat: Dict[str, Any] = {}
        ep_legacy = self.analysis_dir / "entrypoints.json"
        ep_canon = self._canonical_path_for_family("entrypoints")
        if ep_legacy.exists() and ep_canon.exists():
            compat["entrypoints"] = {
                "legacy": _rel(ep_legacy, self.emitted_prefix),
                "canonical": _rel(ep_canon, self.emitted_prefix),
                "note": "legacy alias kept; contents identical",
            }

        header["families"] = dict(sorted(families.items()))
        if compat:
            header["compatibility"] = compat
        return header

    # ---------------- Logging ----------------

    def _log(self, msg: str) -> None:
        print(msg, file=sys.stderr)

# --------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------

def emit_all(repo_root: Path, cfg: Any) -> None:
    """
    Entry point to be called from run_pack.py after the publish step.
    """
    emitter = AnalysisEmitter(repo_root=repo_root, cfg=cfg)
    emitter.run()
