# File: v2/backend/core/utils/code_bundles/code_bundles/src/packager/io/guide_writer.py
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_posix_rel_to(base: Path, target: Path) -> str:
    """
    Return POSIX-style path for `target` relative to `base` if possible.
    Falls back to POSIX absolute if not under base.
    """
    try:
        rel = target.resolve().relative_to(base.resolve())
        return rel.as_posix()
    except Exception:
        return target.as_posix()


class GuideWriter:
    """
    Writes a concise assistant_handoff.v1.json with stable, relative paths and a clear,
    chunked-manifest transport description matching the requested schema.
    """

    def __init__(self, out_path: Path) -> None:
        self.out_path = Path(out_path)

    def write(self, *, cfg: Any) -> None:
        data = self.build(cfg=cfg)
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self.out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def build(self, *, cfg: Any) -> Dict[str, Any]:
        # Resolve primary locations from cfg
        source_root = Path(getattr(cfg, "source_root"))
        out_bundle = Path(getattr(cfg, "out_bundle"))
        out_runspec = Path(getattr(cfg, "out_runspec"))
        out_guide = Path(getattr(cfg, "out_guide"))

        artifact_root = out_bundle.parent
        analysis_dir = artifact_root / "analysis"
        parts_index = artifact_root / f"{getattr(cfg.transport, 'part_stem', 'design_manifest')}_parts_index.json"
        monolith = artifact_root / f"{getattr(cfg.transport, 'part_stem', 'design_manifest')}.jsonl"

        # Helper for consistent relative output paths (relative to repo root)
        rel_artifact_root = _as_posix_rel_to(source_root, artifact_root).rstrip("/") + "/"
        rel_analysis_dir = _as_posix_rel_to(source_root, analysis_dir).rstrip("/") + "/"
        rel_runspec = _as_posix_rel_to(source_root, out_runspec)
        rel_handoff = _as_posix_rel_to(source_root, out_guide)
        rel_parts_index = _as_posix_rel_to(source_root, parts_index)
        rel_monolith = _as_posix_rel_to(source_root, monolith)

        # Transport fields (verbatim from cfg.transport where applicable)
        t = getattr(cfg, "transport")
        transport = {
            "part_stem": str(getattr(t, "part_stem", "design_manifest")),
            "part_ext": str(getattr(t, "part_ext", ".txt")),
            "parts_per_dir": int(getattr(t, "parts_per_dir", 10)),
            "split_bytes": int(getattr(t, "split_bytes", 150000)),
            "preserve_monolith": bool(getattr(t, "preserve_monolith", False)),
            "parts_index": rel_parts_index,
            "monolith": rel_monolith,
        }

        # Analysis files map from cfg.analysis_filenames, normalized to relative paths
        analysis_files: Dict[str, str] = {}
        af_map = getattr(cfg, "analysis_filenames", {}) or {}
        for key, filename in af_map.items():
            analysis_files[key] = rel_analysis_dir + filename

        # Quickstart: fixed order with "why" text; only include if path exists in map
        def _qs_item(k: str, title: str, why: str) -> Dict[str, str] | None:
            p = analysis_files.get(k)
            if not p:
                return None
            return {"title": title, "path": p, "why": why}

        start_here = list(filter(None, [
            _qs_item("entrypoints", "How to run it", "Binary/scripts/CLI entrypoints and how to invoke them."),
            _qs_item("docs", "Docs health", "Docstring coverage by module; obvious gaps."),
            _qs_item("quality", "Complexity hotspots", "Cyclomatic/maintainability signals to prioritize refactors."),
            _qs_item("sql", "SQL surface", "DB schema and query files in one place."),
            _qs_item("git", "Repo provenance", "Branch, last commit, author/date if available."),
        ]))

        data: Dict[str, Any] = {
            "record_type": "assistant_handoff.v1",
            "version": "2",
            "generated_at": _iso_now(),

            "artifact_root": rel_artifact_root,

            "transport": transport,

            "paths": {
                "analysis_dir": rel_analysis_dir,
                "run_spec": rel_runspec,
                "handoff": rel_handoff,
            },

            "analysis_files": analysis_files,

            "quickstart": {
                "start_here": start_here,
                "raw_sources": [
                    {
                        "title": "Open parts index (chunked manifest)",
                        "path": rel_parts_index
                    }
                ]
            },

            "highlights": {
                "stats": {
                    "files_total": None,
                    "python_modules": None,
                    "edges": None
                },
                "top": {
                    "complexity_modules": [],
                    "import_modules": [],
                    "entrypoints": []
                },
                "risks": {
                    "secrets_findings": 0,
                    "license_flags": 0
                }
            },

            "constraints": {
                "offline_only": True
            },

            "limits": {},

            "notes": [
                "All analysis paths are relative to artifact_root.",
                "If preserve_monolith=false, the monolithic manifest may be empty or removed after chunking."
            ]
        }

        return data



