# SPDX-License-Identifier: MIT
# File: backend/core/spine/providers/packager_pack_run.py
from __future__ import annotations

"""
Capability: packager.pack.run.v1
--------------------------------
Wraps the code-bundles Packager as a Spine provider.

Payload schema (all paths may be absolute or relative to CWD):
- src:                str   (REQUIRED)  Staging/mirror root; becomes output/patch_code_bundles/
- out:                str   (REQUIRED)  Output root for bundle and artifacts (design_manifest/)
- ingest:             str   (optional)  External tree to copy into `src` before packaging
- publish_mode:       str   (optional)  "local" | "github" | "both" (default "local")
- gh_owner:           str   (optional)  GitHub owner (when publish_mode ∈ {"github","both"})
- gh_repo:            str   (optional)  GitHub repo  (when publish_mode ∈ {"github","both"})
- gh_branch:          str   (optional)  Git branch (default "main")
- gh_base:            str   (optional)  Base path inside repo (e.g., "subdir")
- gh_token:           str   (optional)  GitHub token with contents:write
- local_publish_root: str   (optional)  Directory to copy artifacts into (publish_mode "local"/"both")
- publish_codebase:   bool  (optional)  Default True
- publish_analysis:   bool  (optional)  Default False
- publish_handoff:    bool  (optional)  Default True
- publish_transport:  bool  (optional)  Default False
- clean_before_publish: bool (optional)  Clean destination before publish (remote/local as applicable)

Return (wrapped by Registry as Result artifact):
{
  "out_bundle":  "<abs path to design_manifest.jsonl>",
  "out_runspec": "<abs path to superbundle.run.json>",
  "out_guide":   "<abs path to assistant_handoff.v1.json>",
  "out_sums":    "<abs path to design_manifest.SHA256SUMS (or empty if github mode)>",
  "src_root":    "<abs src>",
  "ingested":    true|false,
  "published":   {"mode":"local|github|both","code_files_pushed":int,"outputs_pushed":int}
}
"""

from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List
import os
import sys


# ----------------------------- path helpers -----------------------------------
_THIS_FILE = Path(__file__).resolve()


def _project_root() -> Path:
    """
    Heuristic: walk up until we find 'backend' folder.
    Falls back to CWD if not found.
    """
    for p in [_THIS_FILE] + list(_THIS_FILE.parents):
        if (p / "backend").is_dir():
            return p if p.name == "backend" else p
    return Path.cwd().resolve()


def _find_packager_src() -> Path:
    """
    Locate the embedded packager 'src' directory:
      backend/core/utils/code_bundles/code_bundles/src
    """
    root = _project_root()
    cand = root / "backend" / "core" / "utils" / "code_bundles" / "code_bundles" / "src"
    if cand.is_dir():
        return cand.resolve()
    # Also try with optional 'v2' prefix (some bundles keep 'v2/backend/...')
    v2cand = root / "v2" / "backend" / "core" / "utils" / "code_bundles" / "code_bundles" / "src"
    if v2cand.is_dir():
        return v2cand.resolve()
    raise FileNotFoundError(
        "Packager 'src' directory not found under backend/core/utils/code_bundles/code_bundles/src"
    )


def _ensure_on_syspath(p: Path) -> None:
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)


def _bool(v: Any, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "on"}
    return default


# ------------------------------ provider --------------------------------------
def run_v1(task, context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Spine provider entrypoint for 'packager.pack.run.v1'.
    Normalizes payload, calls Packager, and returns a structured Result payload.
    """
    payload: Dict[str, Any] = dict(getattr(task, "payload", {}) or {})

    # -------- required fields
    src_raw = payload.get("src")
    out_raw = payload.get("out")
    if not src_raw or not out_raw:
        raise ValueError("payload must include 'src' and 'out'")

    src = Path(str(src_raw)).expanduser().resolve()
    out = Path(str(out_raw)).expanduser().resolve()

    # -------- optional fields
    ingest = payload.get("ingest")
    ingest_p: Optional[Path] = Path(str(ingest)).expanduser().resolve() if ingest else None

    publish_mode = str(payload.get("publish_mode") or "local").strip().lower()
    if publish_mode not in {"local", "github", "both"}:
        raise ValueError("publish_mode must be one of: local | github | both")

    gh_owner = payload.get("gh_owner")
    gh_repo = payload.get("gh_repo")
    gh_branch = str(payload.get("gh_branch") or "main")
    gh_base = str(payload.get("gh_base") or "")
    gh_token = payload.get("gh_token") or os.getenv("GITHUB_TOKEN") or ""

    local_publish_root = payload.get("local_publish_root")
    lpr_path: Optional[Path] = Path(str(local_publish_root)).expanduser().resolve() if local_publish_root else None

    publish_codebase = _bool(payload.get("publish_codebase"), True)
    publish_analysis = _bool(payload.get("publish_analysis"), False)
    publish_handoff = _bool(payload.get("publish_handoff"), True)
    publish_transport = _bool(payload.get("publish_transport"), False)
    clean_before_publish = _bool(payload.get("clean_before_publish"), False)

    # -------- packager imports (ensure its src is importable like the CLI does)
    packager_src = _find_packager_src()
    _ensure_on_syspath(packager_src)

    # Import Packager and helpers WITHIN the function (after sys.path tweak)
    from packager.core.orchestrator import Packager  # type: ignore
    # Reuse the config builder / helpers from the CLI shim to stay 1:1 with its logic
    from backend.core.utils.code_bundles.code_bundles.run_pack import (  # type: ignore
        build_cfg,
        gather_emitted_paths,
    )

    # -------- derive effective src_dir (mirror behavior from run_pack)
    src_dir = src
    if (src_dir / "codebase").exists():
        src_dir = src_dir / "codebase"

    out.mkdir(parents=True, exist_ok=True)

    # -------- build cfg (no hidden defaults beyond what's explicit above)
    cfg = build_cfg(
        src=src_dir,
        out=out,
        publish_mode=publish_mode,
        gh_owner=str(gh_owner) if gh_owner else None,
        gh_repo=str(gh_repo) if gh_repo else None,
        gh_branch=gh_branch,
        gh_base=gh_base,
        gh_token=str(gh_token) if gh_token else None,
        publish_codebase=publish_codebase,
        publish_analysis=publish_analysis,
        publish_handoff=publish_handoff,
        publish_transport=publish_transport,
        local_publish_root=lpr_path,
        clean_before_publish=clean_before_publish,
    )

    # -------- run packager
    packager = Packager(cfg, rules=None)
    result = packager.run(external_source=ingest_p)

    # Summarize publish counts (best-effort)
    published_info: Dict[str, Any] = {
        "mode": publish_mode,
        "code_files_pushed": None,
        "outputs_pushed": None,
    }
    # When publishing to GitHub, approximate count via emitted paths
    if publish_mode in {"github", "both"}:
        try:
            emitted_paths = gather_emitted_paths(
                src_root=src_dir,
                emitted_prefix=getattr(cfg, "emitted_prefix"),
                exclude_globs=list(getattr(cfg, "exclude_globs", ())),
                segment_excludes=list(getattr(cfg, "segment_excludes", ())),
            )
            published_info["code_files_pushed"] = len(emitted_paths)
            # outputs are two in the current implementation (guide + runspec)
            published_info["outputs_pushed"] = 2
        except Exception:
            pass

    # -------- shape return payload (absolute paths)
    out_bundle = Path(getattr(result, "out_bundle")).resolve()
    out_runspec = Path(getattr(result, "out_runspec")).resolve()
    out_guide = Path(getattr(result, "out_guide")).resolve()
    out_sums = Path(getattr(result, "out_sums")).resolve() if getattr(result, "out_sums", None) else ""

    return {
        "out_bundle": str(out_bundle),
        "out_runspec": str(out_runspec),
        "out_guide": str(out_guide),
        "out_sums": str(out_sums) if out_sums else "",
        "src_root": str(src_dir),
        "ingested": bool(ingest_p is not None),
        "published": published_info,
    }
