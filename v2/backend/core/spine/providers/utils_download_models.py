# SPDX-License-Identifier: MIT
# File: backend/core/spine/providers/utils_download_models.py
from __future__ import annotations

"""
Capability: utils.download_models.v1
------------------------------------
Wraps the local downloader at
  backend/core/utils/downloaders/download_ai_models.py
as a Spine provider (no CLI execution, no side processes).

Behavior
--------
- Imports the downloader module.
- Overrides its configuration (ROOT, HEADERS, optional DIR_STRUCTURE filter).
- Invokes its `main()` function directly.
- Summarizes what exists under the destination after the run.

Payload (all keys optional unless marked REQUIRED)
-------
- dest_root:   str   (REQUIRED)  Directory to write downloads into (replaces module's ROOT)
- hf_token:    str   (optional)  Hugging Face token for Authorization header
- include:     list[str] (optional)  Subtrees to include, by DIR_STRUCTURE key
                                     e.g., ["ai_models/mistral","grammars"]
- dry_run:     bool  (optional)  If True, do not call main(); only summarize.

Return
------
{
  "dest_root": "<abs>",
  "included": ["..."],                # which keys were processed
  "download_invoked": true|false,     # false if dry_run=True
  "summary": {
      "total_files": <int>,
      "total_bytes": <int>,
      "by_dir": [{"path":"<abs>", "files":N, "bytes":M}, ...]
  }
}
"""

from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple
import importlib
import os
import sys


def _as_bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _project_root() -> Path:
    """
    Heuristic: prefer a parent that contains 'backend'.
    Fallback to CWD.
    """
    here = Path(__file__).resolve()
    for p in [here] + list(here.parents):
        if (p / "backend").is_dir():
            return p
    return Path.cwd().resolve()


def _ensure_on_syspath(p: Path) -> None:
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)


def _iter_files(root: Path) -> Iterable[Path]:
    for p in root.rglob("*"):
        try:
            if p.is_file():
                yield p
        except OSError:
            continue


def _summarize_dirs(paths: List[Path]) -> Dict[str, Any]:
    by_dir: List[Dict[str, Any]] = []
    total_files = 0
    total_bytes = 0
    for d in paths:
        files = list(_iter_files(d))
        files_n = len(files)
        bytes_n = 0
        for f in files:
            try:
                bytes_n += f.stat().st_size
            except Exception:
                pass
        by_dir.append({"path": str(d.resolve()), "files": files_n, "bytes": bytes_n})
        total_files += files_n
        total_bytes += bytes_n
    return {
        "total_files": total_files,
        "total_bytes": total_bytes,
        "by_dir": by_dir,
    }


def run_v1(task, context: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = dict(getattr(task, "payload", {}) or {})

    dest_raw = payload.get("dest_root")
    if not dest_raw:
        raise ValueError("payload must include 'dest_root'")
    dest_root = Path(str(dest_raw)).expanduser().resolve()
    dest_root.mkdir(parents=True, exist_ok=True)

    hf_token = payload.get("hf_token")
    include = payload.get("include")
    if include is not None and not isinstance(include, (list, tuple)):
        raise ValueError("'include' must be a list of DIR_STRUCTURE keys if provided")

    dry_run = _as_bool(payload.get("dry_run"), False)

    # Ensure the package root is importable
    proj = _project_root()
    _ensure_on_syspath(proj)

    # Import downloader module
    try:
        mod = importlib.import_module(
            "backend.core.utils.downloaders.download_ai_models"
        )
    except Exception as e:
        raise ImportError(
            "Unable to import backend.core.utils.downloaders.download_ai_models"
        ) from e

    # Validate shapes we expect from the module
    # Required: DIR_STRUCTURE (dict), ROOT (Path or str), HEADERS (dict), main() callable
    if not hasattr(mod, "DIR_STRUCTURE") or not isinstance(mod.DIR_STRUCTURE, dict):
        raise RuntimeError("downloader module is missing DIR_STRUCTURE dict")
    if not hasattr(mod, "main") or not callable(mod.main):
        raise RuntimeError("downloader module is missing callable main()")

    # Override module-level configuration safely
    # 1) Destination root
    mod.ROOT = Path(dest_root)

    # 2) Authorization header if provided
    if hf_token is not None:
        token = str(hf_token).strip()
        if token:
            mod.HEADERS = {"Authorization": f"Bearer {token}"}
        else:
            # Explicitly disable auth header when empty string passed
            mod.HEADERS = {}

    # 3) Optional limiting of which subtrees to process
    if include:
        include_keys = {str(k) for k in include}
        filtered: Dict[str, Any] = {}
        missing: List[str] = []
        for k, v in mod.DIR_STRUCTURE.items():
            if k in include_keys:
                filtered[k] = v
        # Report missing keys if any
        for k in include_keys:
            if k not in mod.DIR_STRUCTURE:
                missing.append(k)
        mod.DIR_STRUCTURE = filtered
        if missing:
            # We choose not to fail hard; we report in the return payload
            missing_info = {"missing_include_keys": missing}
        else:
            missing_info = {}
    else:
        missing_info = {}

    # 4) If dry_run, just summarize what *would* be targeted
    target_dirs = [dest_root / k for k in (mod.DIR_STRUCTURE.keys())]
    if dry_run:
        return {
            "dest_root": str(dest_root),
            "included": list(mod.DIR_STRUCTURE.keys()),
            "download_invoked": False,
            "summary": _summarize_dirs(target_dirs),
            **missing_info,
        }

    # Invoke the module's main() directly (library mode)
    mod.main()  # type: ignore[call-arg]

    # Summarize results from filesystem (what exists now)
    return {
        "dest_root": str(dest_root),
        "included": list(mod.DIR_STRUCTURE.keys()),
        "download_invoked": True,
        "summary": _summarize_dirs(target_dirs),
        **missing_info,
    }
