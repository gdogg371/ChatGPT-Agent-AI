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
    Writes an assistant handoff JSON beside the manifest artifacts.

    Behavior:
      - If the output filename contains ".github." (e.g., "assistant_handoff.github.v1.json"),
        all manifest paths are normalized to the GitHub repo layout:
          [<github.base_path>/]design_manifest/...
      - Otherwise, paths are rooted in the local artifact directory.
    """

    def __init__(self, out_path: Path) -> None:
        self.out_path = Path(out_path)

    def write(self, *, cfg: Any) -> None:
        data = self.build(cfg=cfg)
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        # pretty output; keep key order as constructed (no sort_keys)
        payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        self.out_path.write_text(payload, encoding="utf-8")

    def build(self, *, cfg: Any) -> Dict[str, Any]:
        is_github_handoff = '.github.' in self.out_path.name
        # Anchors
        source_root = Path(getattr(cfg, "source_root"))
        out_bundle = Path(getattr(cfg, "out_bundle"))   # .../design_manifest/design_manifest_0001.txt or .jsonl
        artifact_root = out_bundle.parent               # .../design_manifest/
        analysis_dir = artifact_root / "analysis"
        out_guide = self.out_path

        # Transport / chunking
        t = getattr(cfg, "transport")
        chunked = bool(getattr(t, "chunked", False))
        part_stem = str(getattr(t, "part_stem", "design_manifest"))
        part_ext = str(getattr(t, "part_ext", ".txt"))
        parts_per_dir = int(getattr(t, "parts_per_dir", 10))
        split_bytes = int(getattr(t, "split_bytes", 300_000))
        preserve_monolith = bool(getattr(t, "preserve_monolith", False))

        # Monolith + index paths (local)
        parts_index_path = artifact_root / f"{part_stem}_parts_index.json"
        monolith_path = artifact_root / f"{part_stem}.jsonl"

        # Relative paths (local, relative to source_root)
        rel_artifact_root = _posix_rel(source_root, artifact_root, trailing_slash=True)
        rel_analysis_dir = _posix_rel(source_root, analysis_dir, trailing_slash=True)
        rel_parts_dir = _posix_rel(source_root, artifact_root, trailing_slash=True)
        rel_parts_index = _posix_rel(source_root, parts_index_path)
        rel_monolith = _posix_rel(source_root, monolith_path)
        rel_runspec = _posix_rel(source_root, artifact_root / "superbundle.run.json")
        rel_handoff = _posix_rel(source_root, out_guide)

        # If writing a GitHub-targeted handoff, normalize paths to repo layout (design_manifest/ under optional base_path)
        if is_github_handoff:
            pub = getattr(cfg, "publish", None)
            gh = getattr(pub, "github", None) if pub else None
            base_path = ""
            if gh is not None:
                base_path = str(getattr(gh, "base_path", "") or "")
                if base_path and not base_path.endswith("/"):
                    base_path += "/"
            rel_artifact_root = f"{base_path}design_manifest/"
            rel_analysis_dir = rel_artifact_root + "analysis/"
            rel_parts_dir = rel_artifact_root
            rel_parts_index = rel_artifact_root + f"{part_stem}_parts_index.json"
            rel_monolith = rel_artifact_root + f"{part_stem}.jsonl"
            rel_runspec = rel_artifact_root + "superbundle.run.json"
            rel_handoff = rel_artifact_root + self.out_path.name

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
            github_block = {
                "owner": owner,
                "repo": repo,
                "branch": branch,
                "base_path": base_path,
                "raw_base": raw_base,
            }

        # Quickstart (mode-agnostic; references are relative to artifact_root)
        quickstart = [
            "## Reconstructing the manifest",
            "",
            "1. Read `manifest.paths.chunking.parts_index` and follow the `parts` array order.",
            "2. Concatenate each file under `manifest.paths.chunking.parts_dir` in that exact order.",
            "3. The resulting stream is the monolithic manifest content.",
            "",
            "## Checksums",
            "",
            f'- Monolith checksums: `{rel_artifact_root}design_manifest.SHA256SUMS`',
            f'- Parts checksums:    `{rel_artifact_root}design_manifest.SHA256SUMS`',
            "",
            "To verify (GNU coreutils sha256sum format):",
            "",
            "```bash",
            f'cd "{rel_artifact_root}"  # adjust if needed',
            'sha256sum -c "design_manifest.SHA256SUMS"',
            "```",
        ]

        # Enumerate analysis/** (relative to artifact_root)
        analysis_files = []
        if Path(analysis_dir).exists():
            for p in sorted(Path(analysis_dir).rglob("*")):
                if p.is_file():
                    fname = p.relative_to(analysis_dir).as_posix()
                    analysis_files.append(rel_analysis_dir + fname)

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
                "parts_dir": rel_parts_dir,
            },

            "manifest": {
                "path": rel_monolith,
                "chunking": {
                    "enabled": bool(chunked),
                    "parts_dir": rel_parts_dir,
                    "parts_index": rel_parts_index,
                    "notes": [
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
            },

            "analysis_files": analysis_files,

            "quickstart": quickstart,

            "highlights": {
                "stats": {
                    "files_total": files_total,
                    "python_modules": python_modules,
                },
                "constraints": {"offline_only": True},
                "entrypoints": [],
            },
        }

        return data






