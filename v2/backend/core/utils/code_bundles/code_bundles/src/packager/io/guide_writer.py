# File: v2/backend/core/utils/code_bundles/code_bundles/src/packager/io/guide_writer.py
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class _CfgView:
    # minimal surface we read from the packager config (NS or dict-like)
    out_bundle: Path
    out_runspec: Path
    out_guide: Path

    # publish
    publish_mode: str
    github_owner: Optional[str]
    github_repo: Optional[str]
    github_branch: Optional[str]
    github_base_path: str

    # transport
    transport_kind: str
    part_stem: str
    part_ext: str
    parts_per_dir: int
    split_bytes: int
    preserve_monolith: bool

    # analysis
    analysis_filenames: Dict[str, str]

    # handoff
    reading_order: List[Dict[str, str]]

    # limits
    max_files: Optional[int]
    max_bytes: Optional[int]
    timeout_seconds: Optional[int]


def _dig(obj: Any, path: List[str], default=None):
    cur = obj
    for key in path:
        try:
            if isinstance(cur, dict):
                cur = cur.get(key, default)
            else:
                cur = getattr(cur, key, default)
        except Exception:
            return default
        if cur is None:
            # allow None to be returned early
            pass
    return cur if cur is not None else default


def _ensure_parent(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _load_cfg_view(cfg: Any) -> _CfgView:
    out_bundle = Path(_dig(cfg, ["out_bundle"]))
    out_runspec = Path(_dig(cfg, ["out_runspec"]))
    out_guide = Path(_dig(cfg, ["out_guide"]))

    publish = _dig(cfg, ["publish"], {}) or {}
    mode = str(_dig(publish, ["mode"], "local")).lower()
    github = _dig(publish, ["github"], {}) or {}

    transport = _dig(cfg, ["transport"], {}) or {}
    part_stem = str(_dig(transport, ["part_stem"], "design_manifest"))
    part_ext = str(_dig(transport, ["part_ext"], ".txt"))
    parts_per_dir = int(_dig(transport, ["parts_per_dir"], 10))
    split_bytes = int(_dig(transport, ["split_bytes"], 150000))
    preserve_monolith = bool(_dig(transport, ["preserve_monolith"], False))
    transport_kind = str(_dig(transport, ["kind"], "chunked")).lower()

    analysis_filenames = dict(_dig(cfg, ["analysis_filenames"], {}) or {})

    # handoff reading order (optional)
    reading_order = list(_dig(cfg, ["handoff", "reading_order"], []) or [])

    limits = _dig(cfg, ["limits"], {}) or {}
    max_files = limits.get("max_files")
    max_bytes = limits.get("max_bytes")
    timeout_seconds = limits.get("timeout_seconds")

    return _CfgView(
        out_bundle=out_bundle,
        out_runspec=out_runspec,
        out_guide=out_guide,
        publish_mode=mode,
        github_owner=_dig(github, ["owner"]),
        github_repo=_dig(github, ["repo"]),
        github_branch=_dig(github, ["branch"]),
        github_base_path=str(_dig(github, ["base_path"], "")),
        transport_kind=transport_kind,
        part_stem=part_stem,
        part_ext=part_ext,
        parts_per_dir=parts_per_dir,
        split_bytes=split_bytes,
        preserve_monolith=preserve_monolith,
        analysis_filenames=analysis_filenames,
        reading_order=reading_order,
        max_files=max_files,
        max_bytes=max_bytes,
        timeout_seconds=timeout_seconds,
    )


def _rel_or_abs(base: Path, p: Path) -> str:
    """Return POSIX path; relative to base if inside, else absolute."""
    try:
        rel = p.resolve().relative_to(base.resolve())
        return str((base / rel).as_posix())
    except Exception:
        return str(p.as_posix())


class GuideWriter:
    """
    Produces a richer assistant_handoff.v1.json that:
      - Clearly explains chunked transport (index, parts count, how-to)
      - Enumerates analysis sidecars with canonical names from config
      - Summarizes quickstart and top findings from sidecars (best-effort)
      - Mirrors limits and publish info for downstream consumers
    """

    def __init__(self, out_path: Path) -> None:
        self.out_path = Path(out_path)

    def write(self, *, cfg: Any) -> None:
        data = self.build(cfg=cfg)
        _ensure_parent(self.out_path)
        self.out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # --------------------------------------------------------------------- #
    # Build
    # --------------------------------------------------------------------- #
    def build(self, *, cfg: Any) -> Dict[str, Any]:
        cv = _load_cfg_view(cfg)

        artifact_root = cv.out_bundle.parent
        analysis_dir = artifact_root / "analysis"
        parts_index = artifact_root / f"{cv.part_stem}_parts_index.json"
        parts_dir = artifact_root
        monolith_path = cv.out_bundle

        # Determine parts_count and chunked presence
        chunked = (cv.transport_kind == "chunked")
        parts_count = None
        if parts_index.exists():
            idx = _read_json(parts_index)
            if isinstance(idx, dict):
                # support common shapes: {"parts":[{"path":...}, ...]} or {"paths":[...]}
                if isinstance(idx.get("parts"), list):
                    parts_count = len(idx["parts"])
                elif isinstance(idx.get("paths"), list):
                    parts_count = len(idx["paths"])
        if parts_count is None:
            # fallback: glob by stem/ext
            parts_count = len(list(parts_dir.glob(f"{cv.part_stem}_part_*{cv.part_ext}")))

        monolith_available = monolith_path.exists() and monolith_path.stat().st_size > 0

        # Publish block with optional raw_base
        raw_base = None
        if cv.github_owner and cv.github_repo and cv.github_branch is not None:
            base_path = cv.github_base_path.strip("/")
            suffix = (f"{base_path}/" if base_path else "")
            raw_base = f"https://raw.githubusercontent.com/{cv.github_owner}/{cv.github_repo}/{cv.github_branch}/{suffix}"

        # Analysis files map from config
        analysis_files: Dict[str, str] = {}
        for key, fname in cv.analysis_filenames.items():
            path = analysis_dir / fname
            analysis_files[key] = _rel_or_abs(artifact_root, path)

        # Quickstart: prefer config reading_order if available
        quickstart_blocks = []
        ro = cv.reading_order or []
        for item in ro:
            path = item.get("path") or ""
            why = item.get("why") or ""
            quickstart_blocks.append({
                "title": Path(path).name,
                "path": _rel_or_abs(artifact_root, artifact_root / path),
                "why": why
            })
        if not quickstart_blocks:
            # default sensible quickstart
            for key, title, why in [
                ("entrypoints", "Run & CLI", "How to invoke binaries/scripts and service entrypoints."),
                ("docs", "Docs coverage", "Docstring coverage by module; identify gaps."),
                ("quality", "Complexity hotspots", "Prioritize risky modules/functions."),
                ("sql", "SQL surface", "DB schemas and queries in one place."),
                ("git", "Repo provenance", "Branch, commit, authorship if available."),
            ]:
                p = analysis_files.get(key)
                if p:
                    quickstart_blocks.append({"title": title, "path": p, "why": why})

        # Fill "top" sections from sidecars (best-effort)
        top_complexity = self._top_from_quality(analysis_dir / cv.analysis_filenames.get("quality", "quality.complexity.summary.json"))
        top_imports = self._top_from_imports(analysis_dir / cv.analysis_filenames.get("ast_imports", "ast.imports.summary.json"))
        top_entrypoints = self._top_from_entrypoints(analysis_dir / cv.analysis_filenames.get("entrypoints", "entrypoints.summary.json"))

        # Risks (best-effort counts)
        secrets_count = self._count_from_sidecar(analysis_dir / cv.analysis_filenames.get("secrets", "secrets.summary.json"),
                                                 keys=("total", "findings_total", "findings"))
        license_flags = self._count_from_sidecar(analysis_dir / cv.analysis_filenames.get("license", "license.summary.json"),
                                                 keys=("flags", "non_compliant", "violations"))

        # Stats (best-effort)
        files_total, py_modules, graph_edges = self._stats_best_effort(
            analysis_dir=analysis_dir,
            analysis_files=cv.analysis_filenames
        )

        # Checksums paths (may or may not exist yet)
        checksums = {
            "monolith_sha256": _rel_or_abs(artifact_root, artifact_root / f"{cv.part_stem}.SHA256SUMS"),
            "parts_sha256": _rel_or_abs(artifact_root, artifact_root / "parts.SHA256SUMS"),
            "algo": "sha256",
        }

        data: Dict[str, Any] = {
            "record_type": "assistant_handoff.v1",
            "version": "2",
            "generated_at": _iso_now(),

            "artifact_root": str(artifact_root.as_posix()),

            "publish": {
                "mode": cv.publish_mode,
                "github": {
                    "owner": cv.github_owner,
                    "repo": cv.github_repo,
                    "branch": cv.github_branch,
                    "raw_base": raw_base,
                },
            },

            "transport": {
                "chunked": bool(chunked),
                "part_stem": cv.part_stem,
                "part_ext": cv.part_ext,
                "parts_per_dir": cv.parts_per_dir,
                "split_bytes": cv.split_bytes,
                "preserve_monolith": cv.preserve_monolith,

                "parts_index": _rel_or_abs(artifact_root, parts_index),
                "parts_dir": str(parts_dir.as_posix()),
                "parts_count": int(parts_count),

                "monolith_path": _rel_or_abs(artifact_root, monolith_path),
                "monolith_available": bool(monolith_available),

                "how_to_consume": [
                    "Read parts_index to get ordered part file refs.",
                    "Stream and concatenate part files in the listed order to reconstruct the manifest stream.",
                    "Do NOT lexically sort filenames; always follow the index order."
                ]
            },

            "paths": {
                "analysis_dir": str(analysis_dir.as_posix()),
                "run_spec": _rel_or_abs(artifact_root, cv.out_runspec),
                "handoff": _rel_or_abs(artifact_root, cv.out_guide),
                "checksums": checksums,
            },

            "analysis_files": analysis_files,

            "quickstart": {
                "start_here": quickstart_blocks,
                "raw_manifest": [{
                    "title": "Chunked manifest (parts index)",
                    "path": _rel_or_abs(artifact_root, parts_index),
                    "how": "Follow the listed order to stream parts."
                }]
            },

            "highlights": {
                "stats": {
                    "files_total": files_total,
                    "python_modules": py_modules,
                    "graph_edges": graph_edges
                },
                "top": {
                    "complexity_modules": top_complexity,
                    "import_modules": top_imports,
                    "entrypoints": top_entrypoints
                },
                "risks": {
                    "secrets_findings": secrets_count,
                    "license_flags": license_flags
                }
            },

            "limits": {
                "max_files": cv.max_files,
                "max_bytes": cv.max_bytes,
                "timeout_seconds": cv.timeout_seconds
            },

            "constraints": {
                "offline_only": True
            },

            "notes": [
                "Paths are relative to artifact_root unless absolute.",
                "If preserve_monolith=false, the monolithic manifest may be empty or removed."
            ]
        }
        return data

    # --------------------------------------------------------------------- #
    # Helpers to extract "top" and "stats"
    # --------------------------------------------------------------------- #
    def _top_from_quality(self, path: Path, *, limit: int = 10) -> List[Dict[str, Any]]:
        data = _read_json(path)
        if not data:
            return []

        # Accept either {items:[...]} or a flat list; prefer fields:
        # {path/module, complexity_total|score|cyclomatic_total, functions?}
        items = []
        if isinstance(data, dict):
            for key in ("top_files", "top_modules", "items", "rows"):
                v = data.get(key)
                if isinstance(v, list) and v:
                    items = v
                    break
        elif isinstance(data, list):
            items = data

        scored = []
        for it in items:
            if not isinstance(it, dict):
                continue
            p = it.get("path") or it.get("module") or it.get("name")
            comp = (it.get("complexity_total") or it.get("score") or
                    it.get("cyclomatic_total") or it.get("cyclomatic_max") or 0)
            funcs = it.get("functions") or it.get("function_count") or None
            if p:
                scored.append({"path": p, "complexity_total": comp, "functions": funcs})
        scored.sort(key=lambda x: (x.get("complexity_total") or 0), reverse=True)
        return scored[:limit]

    def _top_from_imports(self, path: Path, *, limit: int = 10) -> List[Dict[str, Any]]:
        data = _read_json(path)
        if not data:
            return []
        # Expect {top_modules:[{name,edges}, ...]} or a list with 'name'/'count'
        mods = []
        if isinstance(data, dict):
            cand = data.get("top_modules") or data.get("modules") or data.get("items")
            if isinstance(cand, list):
                mods = cand
        elif isinstance(data, list):
            mods = data
        out = []
        for it in mods:
            if not isinstance(it, dict):
                continue
            name = it.get("name") or it.get("module") or it.get("path")
            edges = it.get("edges") or it.get("count") or it.get("imports") or 0
            if name:
                out.append({"name": name, "edges": edges})
        out.sort(key=lambda x: (x.get("edges") or 0), reverse=True)
        return out[:limit]

    def _top_from_entrypoints(self, path: Path, *, limit: int = 10) -> List[Dict[str, Any]]:
        data = _read_json(path)
        if not data:
            return []
        items = []
        if isinstance(data, dict):
            items = data.get("items") or data.get("entrypoints") or data.get("rows") or []
        elif isinstance(data, list):
            items = data
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            kind = it.get("kind") or it.get("type") or "entrypoint"
            name = it.get("name") or it.get("command") or it.get("callable") or ""
            path_val = it.get("path") or it.get("module_path") or it.get("module") or ""
            if name or path_val:
                out.append({"kind": kind, "name": name, "path": path_val})
        # Preserve natural order; if there's a score, sort by it
        if out and isinstance(items[0], dict):
            score_key = "score" if "score" in items[0] else None
            if score_key:
                out.sort(key=lambda x: (x.get(score_key) or 0), reverse=True)
        return out[:limit]

    def _count_from_sidecar(self, path: Path, *, keys: tuple[str, ...]) -> int:
        data = _read_json(path)
        if not data:
            return 0
        if isinstance(data, dict):
            for k in keys:
                v = data.get(k)
                if isinstance(v, int):
                    return v
                if isinstance(v, list):
                    return len(v)
        if isinstance(data, list):
            return len(data)
        return 0

    def _stats_best_effort(self, *, analysis_dir: Path, analysis_files: Dict[str, str]) -> tuple[Optional[int], Optional[int], Optional[int]]:
        # Try to infer basic stats from available sidecars.
        files_total = None
        py_modules = None
        graph_edges = None

        # python modules: count unique module/file entries in ast.symbols
        sym_file = analysis_dir / analysis_files.get("ast_symbols", "ast.symbols.summary.json")
        sym = _read_json(sym_file)
        if isinstance(sym, list):
            # dedupe by "path" or "module"
            seen = set()
            for it in sym:
                if isinstance(it, dict):
                    k = it.get("path") or it.get("module") or it.get("file")
                    if k:
                        seen.add(k)
            py_modules = len(seen) if seen else None

        # graph edges: approximate from ast.imports summary length or 'edges' field
        imp_file = analysis_dir / analysis_files.get("ast_imports", "ast.imports.summary.json")
        imp = _read_json(imp_file)
        if isinstance(imp, dict):
            v = imp.get("edges_total") or imp.get("imports_total")
            if isinstance(v, int):
                graph_edges = v
            elif isinstance(imp.get("items"), list):
                graph_edges = len(imp["items"])
        elif isinstance(imp, list):
            graph_edges = len(imp)

        # files_total: approximate via asset summary length
        asset_file = analysis_dir / analysis_files.get("asset", "asset.summary.json")
        assets = _read_json(asset_file)
        if isinstance(assets, list):
            files_total = len(assets)

        return files_total, py_modules, graph_edges

