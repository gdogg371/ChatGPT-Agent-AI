# v2/backend/core/utils/code_bundles/code_bundles/src/packager/analysis_emitter.py

from __future__ import annotations

import json, os, re, sys, hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

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
    new = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == new:
                return _sha256_file(path)
        except Exception:
            pass
    with path.open("w", encoding="utf-8") as f:
        f.write(new)
    return _sha256_file(path)

def _guess_content_type(p: Path) -> str:
    n = p.name.lower()
    if n.endswith(".json"): return "application/json"
    if n.endswith(".ndjson"): return "application/x-ndjson"
    if n.endswith(".txt"): return "text/plain"
    if n.endswith(".yml") or n.endswith(".yaml"): return "text/yaml"
    if n.endswith(".zip"): return "application/zip"
    return "application/octet-stream"

def _rel(p: Path, base: Path) -> str:
    try:
        return p.relative_to(base).as_posix()
    except Exception:
        return p.as_posix()

def _is_enveloped(obj: Any) -> bool:
    return isinstance(obj, dict) and all(k in obj for k in ("kind","version","generated_at_utc","source","stats","items"))

class CfgAdapter:
    def __init__(self, cfg: Any): self._cfg = cfg
    def get(self, path: str, default: Any=None) -> Any:
        cur: Any = self._cfg
        for token in path.split("."):
            if cur is None: return default
            if isinstance(cur, dict): cur = cur.get(token, None)
            else: cur = getattr(cur, token, None)
        return default if cur is None else cur

@dataclass
class RawRecord:
    file: str
    line_no: int
    obj: Dict[str, Any]

class RawManifestIndex:
    def __init__(self, part_dir: Path, part_stem: str="design_manifest", part_ext: str=".txt") -> None:
        self.part_dir, self.part_stem, self.part_ext = part_dir, part_stem, part_ext
        self.parts: List[Path] = []
        self.records: List[RawRecord] = []

    @classmethod
    def discover(cls, emitted_prefix: Path, part_stem: str, part_ext: str) -> "RawManifestIndex":
        pat = re.compile(rf"{re.escape(part_stem)}_\d+_\d+{re.escape(part_ext)}$")
        candidates: List[Path] = []
        for base in (emitted_prefix, emitted_prefix / "design_manifest"):
            if base.exists():
                for p in base.rglob(f"*{part_ext}"):
                    if pat.search(p.name): candidates.append(p.parent)
        part_dir = max(candidates, key=lambda d: len(list(d.glob(f"{part_stem}_*{part_ext}")))) if candidates else emitted_prefix
        idx = cls(part_dir, part_stem, part_ext)
        idx._load()
        return idx

    def _load(self) -> None:
        self.parts = sorted(self.part_dir.glob(f"{self.part_stem}_*{self.part_ext}"))
        for part in self.parts:
            try:
                with part.open("r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        s = line.strip()
                        if not s or s.startswith("#"): continue
                        try:
                            obj = json.loads(s)
                            if isinstance(obj, dict):
                                self.records.append(RawRecord(file=part.name, line_no=i, obj=obj))
                        except Exception:
                            continue
            except Exception:
                continue

class AnalysisEmitter:
    def __init__(self, repo_root: Path, cfg: Any):
        self.repo_root = repo_root.resolve()
        self.cfg = CfgAdapter(cfg)

        self.emitted_prefix = (self.repo_root / str(self.cfg.get("emitted_prefix","output/patch_code_bundles"))).resolve()
        self.analysis_dir = self.emitted_prefix / "analysis"
        self.alt_analysis_dir = (self.repo_root / "output" / "design_manifest" / "analysis")

        self.part_stem = self.cfg.get("transport.part_stem","design_manifest")
        self.part_ext  = self.cfg.get("transport.part_ext",".txt")

        self.meta_emit: Dict[str,str]         = self.cfg.get("metadata_emission",{}) or {}
        self.analysis_filenames: Dict[str,str]= self.cfg.get("analysis_filenames",{}) or {}
        self.family_aliases: Dict[str,str]    = self.cfg.get("family_aliases",{}) or {}

        self.strategy = str(self.cfg.get("controls.analysis_strategy","backfill")).lower()
        self.synthesize_empty = bool(self.cfg.get("controls.synthesize_empty_summaries", True))

        self.publish_analysis = bool(self.cfg.get("publish_analysis", True))
        self.emit_checksums   = bool(self.cfg.get("publish.checksums", False))

        cand = self.emitted_prefix / "design_manifest"
        self.manifest_dir = cand if cand.exists() else self.emitted_prefix

    def run(self) -> None:
        if not self.publish_analysis:
            self._log("[analysis] publish_analysis=false → skip"); return

        raw_idx = RawManifestIndex.discover(self.emitted_prefix, self.part_stem, self.part_ext)
        existing = self._discover_existing_analysis()

        raw_groups = self._group_raw_by_family(raw_idx)

        # Robust target fallback: config -> filenames -> raw -> static defaults
        target_families = {fam for fam,mode in self.meta_emit.items() if str(mode).lower()=="both"}
        if not target_families:
            target_families = set(self.analysis_filenames.keys())
        if not target_families:
            target_families = set(raw_groups.keys())
        if not target_families:
            target_families = {
                "asset","deps","entrypoints","env","git","license","secrets","sql",
                "ast_symbols","ast_imports","ast_calls","docs","quality","html","js","cs","sbom","codeowners"
            }

        written: Dict[str, Tuple[Path,str,int]] = {}
        header = {"packager_version": self.cfg.get("version", None),
                  "config_path": self.cfg.get("_config_path", None),
                  "git": {"branch": self.cfg.get("git_branch", self.cfg.get("publish.github.branch", None)),
                          "commit": self.cfg.get("git_commit", None),
                          "dirty": self.cfg.get("git_dirty", None)}}

        def envelope(fam: str, items: List[Any]) -> Dict[str, Any]:
            return {"kind": f"design_manifest.analysis/{fam}", "version": "1.0",
                    "generated_at_utc": _utcnow(), "source": header, "stats": {"count": len(items)}, "items": items}

        if self.strategy in ("backfill","enforce","passthrough"):
            for fam in sorted(target_families):
                canon = self._canonical_path_for_family(fam, create=True)
                if self.strategy == "enforce":
                    data_items = self._pick_items_for_family(fam, existing, raw_groups)
                else:
                    items = raw_groups.get(fam, [])
                    data_items = items if items else ([] if self.synthesize_empty else None)
                if data_items is None:
                    continue
                doc = envelope(fam, data_items)
                sha = _write_json(canon, doc)
                written[fam] = (canon, sha, len(data_items))
                if fam == "entrypoints":
                    _write_json(self.analysis_dir / "entrypoints.json", doc)

        index = self._build_index(existing, written)
        _write_json(self.analysis_dir / "_index.json", index)

        self._emit_artifact_headers()
        if self.emit_checksums: self._emit_checksums()

        self._log(f"[analysis] strategy={self.strategy} wrote {len(written)} families "
                  f"({', '.join(f'{k}:{v[2]}' for k,v in sorted(written.items()))}); index + headers"
                  f"{' + checksums' if self.emit_checksums else ''}")

    def _discover_existing_analysis(self) -> Dict[str, List[Tuple[Path, Any]]]:
        out: Dict[str, List[Tuple[Path, Any]]] = {}
        for base in (self.analysis_dir, self.alt_analysis_dir):
            if not base.exists(): continue
            inv_map = {fname: fam for fam, fname in self.analysis_filenames.items()}
            for p in base.glob("*.json"):
                try: obj = _read_json(p)
                except Exception: continue
                fam = None
                name = p.name
                if name in inv_map:
                    fam = inv_map[name]
                else:
                    fam = self._infer_family_from_filename(name)
                if fam:
                    fam = self._normalize_family(fam)
                    out.setdefault(fam, []).append((p, obj))
        return out

    def _group_raw_by_family(self, raw_idx: RawManifestIndex) -> Dict[str, List[Dict[str, Any]]]:
        out: Dict[str, List[Dict[str, Any]]] = {}
        for rr in raw_idx.records:
            obj = rr.obj
            fam = self._family_from_raw(obj)
            if not fam: continue
            fam = self._normalize_family(fam)
            out.setdefault(fam, []).append(obj)
        return out

    def _normalize_family(self, t: str) -> str:
        t = (t or "").strip().replace("-", "_")
        fam = self.family_aliases.get(t, t)
        if fam == t and "." in t:
            fam = self.family_aliases.get(t.replace(".","_"), fam)
        return fam

    def _family_from_raw(self, obj: Dict[str, Any]) -> Optional[str]:
        kind = str(obj.get("kind") or "")
        if kind: return kind.split(".",1)[0]
        fam = obj.get("family")
        return str(fam) if fam else None

    def _infer_family_from_filename(self, name: str) -> Optional[str]:
        if name == "entrypoints.json": return "entrypoints"
        m = re.match(r"^([a-zA-Z0-9_]+)\.summary\.json$", name)
        if m: return m.group(1)
        if name.endswith(".index.summary.json"): return name.split(".")[0]
        if name.endswith(".info.summary.json"):  return name.split(".")[0]
        if name.endswith(".calls.summary.json"): return "ast_calls"
        if name.endswith(".imports.summary.json"): return "ast_imports"
        if name.endswith(".symbols.summary.json"):return "ast_symbols"
        if name.endswith(".cyclonedx.json"):     return "sbom"
        return None

    def _canonical_path_for_family(self, fam: str, create: bool=False) -> Path:
        fname = self.analysis_filenames.get(fam, f"{fam}.summary.json")
        p = self.analysis_dir / fname
        if create: p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def _pick_items_for_family(self, fam: str,
                               existing: Dict[str, List[Tuple[Path, Any]]],
                               raw_groups: Dict[str, List[Dict[str, Any]]]) -> List[Any]:
        canon = self._canonical_path_for_family(fam)
        if canon.exists():
            try: obj = _read_json(canon)
            except Exception: obj = None
            if obj is not None:
                return obj["items"] if _is_enveloped(obj) else (obj if isinstance(obj, list) else [obj])
        if fam in existing and existing[fam]:
            obj = existing[fam][0][1]
            return obj["items"] if _is_enveloped(obj) else (obj if isinstance(obj, list) else [obj])
        if fam in raw_groups: return raw_groups[fam]
        return [] if self.synthesize_empty else None  # type: ignore

    def _emit_artifact_headers(self) -> None:
        part_pat = re.compile(rf"{re.escape(self.part_stem)}_\d+_\d+{re.escape(self.part_ext)}$")
        for p in self.emitted_prefix.rglob("*"):
            if not p.is_file(): continue
            rel = _rel(p, self.emitted_prefix)
            if rel.startswith("analysis/"): continue
            if p.name.endswith(".header.json"): continue
            if p.name == "design_manifest.SHA256SUMS": continue
            if part_pat.match(p.name): continue
            hdr = {"path": rel, "size": p.stat().st_size, "sha256": _sha256_file(p),
                   "created_utc": _utcnow(), "content_type": _guess_content_type(p)}
            _write_json(p.with_name(p.name + ".header.json"), hdr)

    def _emit_checksums(self) -> None:
        primary_dir = self.manifest_dir if self.manifest_dir.exists() else self.emitted_prefix
        lines: List[str] = []
        for p in sorted(self.emitted_prefix.rglob("*")):
            if not p.is_file(): continue
            if p.name == "design_manifest.SHA256SUMS": continue
            lines.append(f"{_sha256_file(p)} *{_rel(p, self.emitted_prefix)}")
        sums_path = primary_dir / "design_manifest.SHA256SUMS"
        _write_text(sums_path, "\n".join(lines) + "\n")

        legacy = self.repo_root / "output" / "design_manifest"
        if legacy.exists():
            _write_text(legacy / "design_manifest.SHA256SUMS", "\n".join(lines) + "\n")

    def _build_index(self,
                     existing_paths: Dict[str, List[Tuple[Path, Any]]],
                     newly_written: Dict[str, Tuple[Path,str,int]]) -> Dict[str, Any]:
        families: Dict[str, Dict[str, Any]] = {}
        for fam,(path,sha,count) in newly_written.items():
            families[fam] = {"path": _rel(path, self.emitted_prefix), "sha256": sha, "count": count}
        if self.analysis_dir.exists():
            for p in self.analysis_dir.glob("*.json"):
                if p.name == "_index.json": continue
                if any(f["path"] == _rel(p, self.emitted_prefix) for f in families.values()):
                    continue
                try:
                    obj = _read_json(p); cnt = len(obj["items"]) if _is_enveloped(obj) else (len(obj) if isinstance(obj,list) else 1)
                except Exception:
                    cnt = 0
                families.setdefault(self._infer_family_from_filename(p.name) or p.stem.split(".")[0],
                                    {"path": _rel(p, self.emitted_prefix), "sha256": _sha256_file(p), "count": cnt})
        if self.alt_analysis_dir.exists():
            for p in self.alt_analysis_dir.glob("*.json"):
                alias = self._infer_family_from_filename(p.name) or p.stem.split(".")[0]
                if alias not in families:
                    try:
                        obj = _read_json(p); cnt = len(obj["items"]) if _is_enveloped(obj) else (len(obj) if isinstance(obj,list) else 1)
                    except Exception:
                        cnt = 0
                    families[alias] = {"path": _rel(p, self.emitted_prefix), "sha256": _sha256_file(p), "count": cnt}
        return {"kind":"design_manifest.analysis/index","version":"1.0","generated_at_utc":_utcnow(),
                "families": dict(sorted(families.items()))}

    def _log(self, msg: str) -> None:
        print(msg, file=sys.stderr)

def emit_all(repo_root: Path, cfg: Any) -> None:
    AnalysisEmitter(repo_root=repo_root, cfg=cfg).run()



