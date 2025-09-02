# File: v2/backend/core/utils/code_bundles/code_bundles/src/packager/io/guide_writer.py
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def _iso_now() -> str:
    # ISO-8601 UTC with Z suffix
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _posix_rel(base: Path, target: Path, *, trailing_slash: bool = False) -> str:
    """
    POSIX path for `target` relative to `base` where possible; otherwise absolute.
    Optionally ensure a trailing slash.
    """
    base = base.resolve()
    target = target.resolve()
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


class GuideWriter:
    """
    Emit assistant_handoff.v1.json matching the target schema (no checksum writing).
    - Preserves the original top-level structure: transport, paths, analysis_files (map),
      quickstart cards, highlights, limits, constraints, notes.
    - If output filename contains ".github." (e.g., assistant_handoff.github.v1.json),
      all paths are normalized to the GitHub repo layout: [<github.base_path>/]design_manifest/...
    """

    def __init__(self, out_path: Path) -> None:
        self.out_path = Path(out_path)

    def write(self, *, cfg: Any) -> None:
        data = self.build(cfg=cfg)
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        self.out_path.write_text(payload, encoding="utf-8")

    def build(self, *, cfg: Any) -> Dict[str, Any]:
        is_github_handoff = ".github." in self.out_path.name

        # Anchors
        source_root = Path(getattr(cfg, "source_root"))
        out_bundle = Path(getattr(cfg, "out_bundle"))   # .../design_manifest/design_manifest.jsonl or part file
        artifact_root = out_bundle.parent               # .../design_manifest/
        out_runspec = Path(getattr(cfg, "out_runspec", artifact_root / "superbundle.run.json"))
        out_guide = Path(getattr(cfg, "out_guide", self.out_path))
        analysis_dir = artifact_root / "analysis"

        # Transport config (reflect runtime; default split to 150_000 if unset)
        t = getattr(cfg, "transport")
        part_stem = str(getattr(t, "part_stem", "design_manifest"))
        part_ext = str(getattr(t, "part_ext", ".txt"))
        parts_per_dir = int(getattr(t, "parts_per_dir", 10))
        split_bytes = int(getattr(t, "split_bytes", 150_000))
        preserve_monolith = bool(getattr(t, "preserve_monolith", False))

        parts_index_path = artifact_root / f"{part_stem}_parts_index.json"
        parts_index = _read_json(parts_index_path) or {}
        parts_count = int(parts_index.get("total_parts") or 0)
        chunked = parts_count > 0

        monolith_path = artifact_root / f"{part_stem}.jsonl"
        monolith_available = monolith_path.exists()

        # Relative (repo-root) POSIX paths (local layout by default)
        rel_artifact_root = _posix_rel(source_root, artifact_root, trailing_slash=True)
        rel_analysis_dir = _posix_rel(source_root, analysis_dir, trailing_slash=True)
        rel_runspec = _posix_rel(source_root, out_runspec)
        rel_handoff = _posix_rel(source_root, out_guide)
        rel_parts_index = _posix_rel(source_root, parts_index_path)
        rel_monolith = _posix_rel(source_root, monolith_path)

        # If output filename indicates GitHub variant, switch to repo layout under repo root
        if ".github." in self.out_path.name:
            pub = getattr(cfg, "publish", None)
            gh = getattr(pub, "github", None) if pub else None
            base_path = str(getattr(gh, "base_path", "") or "")
            if base_path and not base_path.endswith("/"):
                base_path += "/"

            # In-repo layout: design_manifest/ sits at repo root (or under base_path if set)
            rel_artifact_root = f"{base_path}design_manifest/"
            rel_analysis_dir  = rel_artifact_root + "analysis/"
            rel_runspec       = rel_artifact_root + "superbundle.run.json"
            rel_handoff       = rel_artifact_root + self.out_path.name
            rel_parts_index   = rel_artifact_root + f"{part_stem}_parts_index.json"
            rel_monolith      = rel_artifact_root + f"{part_stem}.jsonl"

        # Publish block
        pub = getattr(cfg, "publish", None)
        mode = (getattr(pub, "mode", None) or "local").lower() if pub else "local"
        gh = getattr(pub, "github", None)
        github_block = None
        if gh and getattr(gh, "owner", None) and getattr(gh, "repo", None):
            owner = str(getattr(gh, "owner"))
            repo = str(getattr(gh, "repo"))
            branch = str(getattr(gh, "branch", "main"))
            base_path = str(getattr(gh, "base_path", "") or "")
            if base_path and not base_path.endswith("/"):
                base_path += "/"
            raw_base = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{base_path}"
            github_block = {"owner": owner, "repo": repo, "branch": branch, "raw_base": raw_base}

        # Canonical analysis_files (include only files that exist)
        expected = {
            "asset": "asset.summary.json",
            "deps": "deps.scan.summary.json",
            "entrypoints": "entrypoints.summary.json",
            "env": "env.summary.json",
            "git": "git.info.summary.json",
            "license": "license.summary.json",
            "secrets": "secrets.summary.json",
            "sql": "sql.index.summary.json",
            "ast_symbols": "ast.symbols.summary.json",
            "ast_imports": "ast.imports.summary.json",
            "ast_calls": "ast.calls.summary.json",
            "docs": "docs.coverage.summary.json",
            "quality": "quality.complexity.summary.json",
            "html": "html.summary.json",
            "js": "js.index.summary.json",
            "cs": "cs.summary.json",
            "sbom": "sbom.cyclonedx.json",
            "manifest": "manifest.summary.json",
            "codeowners": "codeowners.summary.json",
        }
        analysis_files: Dict[str, str] = {}
        for k, fname in expected.items():
            fp = analysis_dir / fname
            if fp.exists():
                analysis_files[k] = rel_analysis_dir + fname

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
                    "how": "Follow the listed order to stream parts.",
                }
            ],
        }

        # Optional highlights from analysis/_index.json (best-effort)
        idx = _read_json(analysis_dir / "_index.json") or {}
        families = idx.get("families") or {}
        files_total = (families.get("asset") or {}).get("count")
        python_modules = (families.get("ast_symbols") or {}).get("count")

        data: Dict[str, Any] = {
            "record_type": "assistant_handoff.v1",
            "version": "2",
            "generated_at": _iso_now(),

            "artifact_root": rel_artifact_root,

            "publish": {"mode": mode, **({"github": github_block} if github_block else {})},

            "transport": {
                "chunked": bool(chunked),
                "part_stem": part_stem,
                "part_ext": part_ext,
                "parts_per_dir": parts_per_dir,
                "split_bytes": split_bytes,
                "preserve_monolith": preserve_monolith,

                "parts_index": rel_parts_index,
                "parts_dir": rel_artifact_root,
                "parts_count": parts_count,

                "monolith_path": rel_monolith,
                "monolith_available": bool(monolith_available),

                "how_to_consume": [
                    "Read parts_index to get ordered part file refs.",
                    "Stream and concatenate part files in the listed order to reconstruct the manifest stream.",
                    "Do NOT lexically sort filenames; always follow the index order.",
                ],
            },

            "paths": {
                "analysis_dir": rel_analysis_dir,
                "run_spec": rel_runspec,
                "handoff": rel_handoff,
                "checksums": {
                    "monolith_sha256": rel_artifact_root + "design_manifest.SHA256SUMS",
                    "parts_sha256":   rel_artifact_root + "design_manifest.SHA256SUMS",
                    "algo": "sha256",
                },
            },

            "analysis_files": analysis_files,

            "quickstart": quickstart,

            "highlights": {
                "stats": {
                    "files_total": files_total,
                    "python_modules": python_modules,
                    "graph_edges": None,
                },
                "top": {
                    "complexity_modules": [],
                    "import_modules": [],
                    "entrypoints": [],
                },
                "risks": {
                    "secrets_findings": 0,
                    "license_flags": 0,
                },
            },

            "limits": {"max_files": 0, "max_bytes": 0, "timeout_seconds": 600},
            "constraints": {"offline_only": True},
            "notes": [
                "Paths are relative to artifact_root unless absolute.",
                "If preserve_monolith=false, the monolithic manifest may be empty or removed.",
            ],
        }

        return data








