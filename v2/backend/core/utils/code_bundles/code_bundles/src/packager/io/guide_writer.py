from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, List


def _iso_now() -> str:
    """ISO-8601 UTC with Z suffix."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _posix_rel(base: Path, target: Path, *, trailing_slash: bool = False) -> str:
    """
    Return a POSIX-style path for `target` relative to `base` where possible;
    otherwise return an absolute POSIX path. Optionally ensure a trailing slash.
    """
    base = Path(base).resolve()
    target = Path(target).resolve()
    try:
        p = target.relative_to(base).as_posix()
    except Exception:
        p = target.as_posix()
    if trailing_slash and not p.endswith("/"):
        p += "/"
    return p


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _take(lst: List[Any], n: int) -> List[Any]:
    return lst[:n] if isinstance(lst, list) else []


def _get(obj, key, default=None):
    """Read config key from either a dict or an object via attribute."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _coerce_str(v) -> Optional[str]:
    """
    Coerce common config value shapes to a string:
    - str/Path/int/bool -> str
    - dict -> prefer keys: path, filename, file, name, value; else first string-like value
    """
    if v is None:
        return None
    if isinstance(v, (str, Path)):
        return str(v)
    if isinstance(v, (int, float, bool)):
        return str(v)
    if isinstance(v, dict):
        for k in ("path", "filename", "file", "name", "value"):
            if k in v and isinstance(v[k], (str, Path, int, float, bool)):
                return str(v[k])
        for val in v.values():
            if isinstance(val, (str, Path, int, float, bool)):
                return str(val)
    return None


def _req_str(v, label: str) -> str:
    s = _coerce_str(v)
    if not s:
        raise RuntimeError(f"GuideWriter: {label} must be a string-like value; got {type(v).__name__}")
    return s


def _coerce_int(v, default: int) -> int:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        try:
            return int(v.strip())
        except Exception:
            return default
    if isinstance(v, dict):
        for k in ("value", "count", "n", "int"):
            if k in v:
                try:
                    return int(v[k])
                except Exception:
                    pass
    return default


def _coerce_bool(v, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(v, dict):
        for k in ("value", "enabled", "on"):
            if k in v:
                return _coerce_bool(v[k], default)
    return default


class GuideWriter:
    """
    Writes an `assistant_handoff.v1` JSON guide that points to the produced
    design manifest artifacts and selected analysis sidecars.

    Strict filename policy (no hard-coded literals inside the code):
      - Uses cfg.manifest_paths.* for all well-known filenames:
          analysis_subdir, parts_index_filename, analysis_index_filename,
          python_index_filename (all relative to artifact_root / analysis_dir).
      - Uses cfg.publish.runspec_filename (or cfg.out_runspec absolute) for run-spec.
      - Uses cfg.publish.handoff_filename (or cfg.out_guide) for the guide path.
      - Uses cfg.transport.monolith_ext (default: ".jsonl") for monolith name.
      - Uses cfg.analysis_filenames[...] for all analysis sidecar files.
    """

    def __init__(self, out_path: Path) -> None:
        self.out_path = Path(out_path)

    def write(self, *, cfg: Any) -> None:
        data = self.build(cfg=cfg)
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        self.out_path.write_text(payload, encoding="utf-8")

    def build(self, *, cfg: Any) -> Dict[str, Any]:
        # Anchors
        source_root = Path(_req_str(_get(cfg, "source_root", None), "source_root"))
        out_bundle = Path(_req_str(_get(cfg, "out_bundle", None), "out_bundle"))  # .../<root_dir>/<monolith or part>
        artifact_root = out_bundle.parent  # .../<root_dir>/
        out_guide = Path(_coerce_str(_get(cfg, "out_guide", None)) or self.out_path)

        # ────────────────────────────────────────────────────────────────────
        # Resolve manifest paths (strict: no filename-based discovery)
        # ────────────────────────────────────────────────────────────────────
        mp = _get(cfg, "manifest_paths", None)

        # root_dir_name is used for GitHub-relative paths; infer from artifact_root if absent
        root_dir_name = _coerce_str(_get(mp, "root_dir", None)) or artifact_root.name

        # analysis directory (strict)
        analysis_subdir = _coerce_str(_get(mp, "analysis_subdir", None))
        if analysis_subdir:
            analysis_dir = artifact_root / analysis_subdir
        else:
            # legacy: explicit absolute/relative analysis_out_dir allowed
            analysis_out_dir = _get(cfg, "analysis_out_dir", None)
            if isinstance(analysis_out_dir, (str, Path)) or isinstance(analysis_out_dir, dict):
                analysis_dir = Path(_req_str(analysis_out_dir, "analysis_out_dir"))
            else:
                raise RuntimeError(
                    "GuideWriter: manifest_paths.analysis_subdir or analysis_out_dir must be set; "
                    "filename-based discovery is disabled."
                )

        # parts index (strict)
        parts_index_filename = _coerce_str(_get(mp, "parts_index_filename", None))
        if parts_index_filename:
            parts_index_path = artifact_root / parts_index_filename
        else:
            pip = _get(cfg, "parts_index_path", None)
            if isinstance(pip, (str, Path)) or isinstance(pip, dict):
                parts_index_path = Path(_req_str(pip, "parts_index_path"))
            else:
                raise RuntimeError(
                    "GuideWriter: manifest_paths.parts_index_filename or parts_index_path must be set; "
                    "discovery/fallback disabled."
                )

        # checksums filename/path (kept lenient by design)
        checksums_filename = _coerce_str(_get(mp, "checksums_filename", None))
        if checksums_filename:
            checksums_path = artifact_root / checksums_filename
        else:
            csp = _get(cfg, "checksums_path", None)
            if isinstance(csp, (str, Path)) or isinstance(csp, dict):
                checksums_path = Path(_req_str(csp, "checksums_path"))
            else:
                # discover *.SHA256SUMS as a neutral fallback
                sums = list(artifact_root.glob("*.SHA256SUMS"))
                checksums_path = sums[0] if sums else artifact_root / "SHA256SUMS"

        # ────────────────────────────────────────────────────────────────────
        # Transport / split behavior
        # ────────────────────────────────────────────────────────────────────
        t = _get(cfg, "transport", {}) or {}
        part_stem = _coerce_str(_get(t, "part_stem", None)) or Path(out_bundle).stem
        part_ext = _coerce_str(_get(t, "part_ext", None)) or ".txt"
        parts_per_dir = _coerce_int(_get(t, "parts_per_dir", None), 10)
        split_bytes = _coerce_int(_get(t, "split_bytes", None), 150_000)
        preserve_monolith = _coerce_bool(_get(t, "preserve_monolith", None), False)
        monolith_ext = _coerce_str(_get(t, "monolith_ext", None)) or ".jsonl"

        parts_index = _read_json(parts_index_path) or {}
        parts_count = int(parts_index.get("total_parts") or 0)
        chunked = parts_count > 0

        # Monolith path (if produced by your chunker, it should match part_stem + monolith_ext)
        monolith_path = artifact_root / f"{part_stem}{monolith_ext}"
        monolith_available = monolith_path.exists()

        # ────────────────────────────────────────────────────────────────────
        # Run-spec (strict: either explicit or configured filename; no discovery)
        # ────────────────────────────────────────────────────────────────────
        out_runspec_raw = _get(cfg, "out_runspec", None)
        if out_runspec_raw:
            out_runspec = Path(_req_str(out_runspec_raw, "out_runspec"))
        else:
            pub = _get(cfg, "publish", None)
            runspec_filename = _coerce_str(_get(pub, "runspec_filename", None))
            if not runspec_filename:
                raise RuntimeError(
                    "GuideWriter: provide either cfg.out_runspec or publish.runspec_filename; discovery disabled."
                )
            out_runspec = artifact_root / runspec_filename

        # ────────────────────────────────────────────────────────────────────
        # Relative paths (local variant)
        # ────────────────────────────────────────────────────────────────────
        rel_artifact_root = _posix_rel(source_root, artifact_root, trailing_slash=True)
        rel_analysis_dir = _posix_rel(source_root, analysis_dir, trailing_slash=True)
        rel_runspec = _posix_rel(source_root, out_runspec)
        rel_handoff = _posix_rel(source_root, out_guide)
        rel_parts_index = _posix_rel(source_root, parts_index_path)
        rel_monolith = _posix_rel(source_root, monolith_path)

        # analysis/python index filenames from config (strict presence of names; files may not exist)
        analysis_index_filename = _coerce_str(_get(mp, "analysis_index_filename", None))
        python_index_filename = _coerce_str(_get(mp, "python_index_filename", None))
        if not analysis_index_filename:
            raise RuntimeError("GuideWriter: manifest_paths.analysis_index_filename must be set.")
        if not python_index_filename:
            raise RuntimeError("GuideWriter: manifest_paths.python_index_filename must be set.")

        rel_analysis_index = _posix_rel(source_root, analysis_dir / analysis_index_filename)
        rel_checksums = _posix_rel(source_root, checksums_path)

        # ────────────────────────────────────────────────────────────────────
        # GitHub variant (switch to in-repo layout under base_path)
        # ────────────────────────────────────────────────────────────────────
        if ".github." in self.out_path.name:
            pub = _get(cfg, "publish", None)
            gh = _get(pub, "github", None)
            base_path = _coerce_str(_get(gh, "base_path", "")) or ""
            if base_path and not base_path.endswith("/"):
                base_path += "/"

            # In-repo layout: <root_dir_name>/ sits at repo root (or under base_path if set)
            rel_artifact_root = f"{base_path}{root_dir_name.rstrip('/')}/"

            # Determine analysis subdir name relative to artifact root for GitHub paths
            try:
                analysis_subdir_name = Path(analysis_dir).relative_to(artifact_root).as_posix()
            except Exception:
                analysis_subdir_name = Path(analysis_dir).name
            if analysis_subdir_name and not analysis_subdir_name.endswith("/"):
                analysis_subdir_name += "/"

            rel_analysis_dir = rel_artifact_root + analysis_subdir_name
            rel_runspec = rel_artifact_root + Path(out_runspec).name
            rel_handoff = rel_artifact_root + self.out_path.name
            rel_parts_index = rel_artifact_root + Path(parts_index_path).name
            rel_monolith = rel_artifact_root + f"{part_stem}{monolith_ext}"
            rel_analysis_index = rel_analysis_dir + analysis_index_filename
            rel_checksums = rel_artifact_root + Path(checksums_path).name

        # Publish block details (for the "publish" field and raw_base)
        pub = _get(cfg, "publish", None)
        mode = (_coerce_str(_get(pub, "mode", None)) or "local").lower() if pub else "local"
        gh = _get(pub, "github", None)
        github_block = None
        if gh and _get(gh, "owner", None) and _get(gh, "repo", None):
            owner = _req_str(_get(gh, "owner", None), "publish.github.owner")
            repo = _req_str(_get(gh, "repo", None), "publish.github.repo")
            branch = _coerce_str(_get(gh, "branch", None)) or "main"
            base_path = _coerce_str(_get(gh, "base_path", "")) or ""
            if base_path and not base_path.endswith("/"):
                base_path += "/"
            raw_base = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{base_path}{root_dir_name}/"
            github_block = {"owner": owner, "repo": repo, "branch": branch, "raw_base": raw_base}

        # --- analysis_files: use ONLY config mapping (no hardcoded dict)
        # Expecting cfg.analysis_filenames: Dict[str, str|dict] (relative to analysis_dir)
        analysis_filenames_raw = _get(cfg, "analysis_filenames", {}) or {}
        analysis_files: Dict[str, str] = {}
        if isinstance(analysis_filenames_raw, dict):
            for k, spec in analysis_filenames_raw.items():
                fname = _coerce_str(spec)
                if not fname:
                    continue
                fp = analysis_dir / fname
                if fp.exists():
                    analysis_files[str(k)] = rel_analysis_dir + fname

        # Quickstart (include cards only if files exist)
        def _card(title: str, key: str, why: str):
            p = analysis_files.get(key)
            return {"title": title, "path": p, "why": why} if p else None

        quickstart = {
            "start_here": list(filter(None, [
                _card("Run & CLI", "entrypoints", "How to invoke binaries/scripts and service entrypoints."),
                _card("Docs coverage", "docs", "Docstring coverage by module; identify gaps."),
                _card("Complexity hotspots", "quality", "Prioritize risky modules/functions."),
                _card("SQL surface", "sql", "DB schemas and queries in one place."),
                _card("Repo provenance", "git", "Branch, commit, authorship if available."),
            ])),
            "raw_manifest": [
                {
                    "title": "Chunked manifest (parts index)",
                    "path": rel_parts_index,
                    "why": "Top-level index for chunked manifest. Enumerates parts and sizes.",
                },
                {
                    "title": "Monolith manifest",
                    "path": rel_monolith if monolith_available else None,
                    "why": "Full manifest in one file (if preserved).",
                },
                {
                    "title": "Run spec",
                    "path": rel_runspec,
                    "why": "Inputs, controls and provenance for this run.",
                },
                {
                    "title": "Checksums",
                    "path": rel_checksums,
                    "why": "Integrity verification for manifest files.",
                },
                {
                    "title": "Analysis index",
                    "path": rel_analysis_index,
                    "why": "Families and counts for analysis outputs.",
                },
                {
                    "title": "This guide",
                    "path": rel_handoff,
                    "why": "You're reading it!",
                },
            ]
        }

        # Highlights / summary (read-only; guard if files absent)
        # analysis index
        idx = _read_json(analysis_dir / analysis_index_filename) or {}
        families = idx.get("families") if isinstance(idx, dict) else None

        # python index (None-safe)
        py_index = _read_json(analysis_dir / python_index_filename) or {}
        if not isinstance(py_index, dict):
            py_index = {}
        py_summary = (py_index.get("summary") or {})
        if not isinstance(py_summary, dict):
            py_summary = {}

        files_total_python = (py_summary.get("files_total") or py_summary.get("files") or 0)
        python_functions = (py_summary.get("total_functions") or py_summary.get("functions") or 0)
        python_classes = (py_summary.get("total_classes") or py_summary.get("classes") or 0)
        python_loc = (py_summary.get("total_loc") or py_summary.get("loc") or 0)

        # AST/Imports stats
        ast_symbols = _read_json(analysis_dir / (_coerce_str(analysis_filenames_raw.get("ast_symbols")) or "")) or {}
        ast_imports = _read_json(analysis_dir / (_coerce_str(analysis_filenames_raw.get("ast_imports")) or "")) or {}
        python_symbols = ((ast_symbols.get("stats") or {}).get("count")) if isinstance(ast_symbols, dict) else None
        imports_statements = ((ast_imports.get("stats") or {}).get("count")) if isinstance(ast_imports, dict) else None
        top_import_modules = (
            (ast_imports.get("stats") or {}).get("top_modules")
            if isinstance(ast_imports, dict) else None
        )

        # Complexity hotspots
        quality = _read_json(analysis_dir / (_coerce_str(analysis_filenames_raw.get("quality")) or "")) or {}
        heavy_files_top = (
            quality.get("heavy_files_top")
            or (quality.get("stats") or {}).get("heavy_files_top")
            or []
        )

        # Entrypoints
        entrypoints = _read_json(analysis_dir / (_coerce_str(analysis_filenames_raw.get("entrypoints")) or "")) or {}
        entry_items = entrypoints.get("items") if isinstance(entrypoints, dict) else None

        # SQL graph (optional)
        sql_refs = _read_json(analysis_dir / (_coerce_str(analysis_filenames_raw.get("sql_refs")) or "")) or {}
        sql_graph_edges = len(sql_refs.get("edges") or []) if isinstance(sql_refs, dict) else None

        # Risks (counts from families, if available)
        secrets_count = ((families.get("secrets") or {}).get("count")) if isinstance(families, dict) else None
        license_count = ((families.get("license") or {}).get("count")) if isinstance(families, dict) else None

        highlights = {
            "stats": {
                "files_total_python": files_total_python,
                "python_functions": python_functions,
                "python_classes": python_classes,
                "python_loc": python_loc,
                "python_symbols": python_symbols,
                "imports_statements": imports_statements,
                "sql_graph_edges": sql_graph_edges,
            },
            "top": {
                "complexity_modules": _take(heavy_files_top, 5),
                "import_modules": _take(top_import_modules or [], 5),
                "entrypoints": _take(entry_items or [], 5),
            },
            "risks": {
                "secrets_findings": secrets_count or 0,
                "license_flags": license_count or 0,
            },
        }

        data: Dict[str, Any] = {
            "record_type": "assistant_handoff.v1",
            "version": "2",
            "generated_at": _iso_now(),

            "artifact_root": rel_artifact_root,

            "publish": {
                "mode": mode,
                **({"github": github_block} if github_block else {}),
            },

            "transport": {
                "chunked": chunked,
                "part_stem": part_stem,
                "part_ext": part_ext,
                "parts_per_dir": parts_per_dir,
                "split_bytes": split_bytes,
                "preserve_monolith": preserve_monolith,
            },

            "paths": {
                "source_root": _posix_rel(source_root, source_root, trailing_slash=True),
                "guide": rel_handoff,
                "runspec": rel_runspec,
                "checksums": rel_checksums,
                "parts_index": rel_parts_index,
                "monolith": rel_monolith if monolith_available else None,
                "analysis_index": rel_analysis_index,
            },

            "analysis_files": analysis_files,

            "quickstart": quickstart,

            "highlights": highlights,

            "limits": {"max_files": 0, "max_bytes": 0, "timeout_seconds": 600},
            "constraints": {"offline_only": True},
            "notes": [
                "Paths are relative to artifact_root unless absolute.",
                "If preserve_monolith=false, the monolithic manifest may be empty or removed.",
            ],
        }

        return data
