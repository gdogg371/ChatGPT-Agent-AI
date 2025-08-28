# File: v2/backend/core/run/docstrings.py
"""
Docstrings runner (pipeline-first, capability-routed).

What this does:
  1) Resolves project_root by locating config/spine/capabilities.yml upwards.
  2) Loads your active vars file (YAML) so the engine receives DB + LLM settings:
       - sqlalchemy_url, sqlalchemy_table, status, max_rows
       - provider, model, ask_spec
       - out_base, out_file (if present)
       - any other pipeline knobs you keep in vars.yml
  3) Invokes the executor engine (which calls capabilities for FETCH → ENRICH → BUILD → LLM
     → SANITIZE → VERIFY → PATCH).

Usage:
    python -m v2.backend.core.run.docstrings

Optional env overrides:
    SPINE_VARS_FILE   → full path to a vars.yml to load
    SPINE_CAPS_FILE   → full path to capabilities.yml (for the info log only)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import yaml  # type: ignore
except Exception as e:  # pragma: no cover
    raise RuntimeError("PyYAML is required to run the docstrings pipeline (pip install pyyaml)") from e

from v2.backend.core.prompt_pipeline.executor.engine import run_v1 as engine_run_v1


# -------------------------- discovery helpers --------------------------

_THIS = Path(__file__).resolve()


def _find_project_root(start: Path) -> Path:
    """
    Walk upwards from 'start' until we find config/spine/capabilities.yml.
    Fallback to start's ancestor that looks like the repo root (directory named containing a 'v2' dir).
    """
    p = start
    while True:
        caps = p / "config" / "spine" / "capabilities.yml"
        if caps.exists():
            return p
        if p.parent == p:
            break
        p = p.parent
    # Heuristic fallback: repo root is parent of 'v2'
    for up in start.parents:
        if (up / "v2").exists():
            return up
    return start


def _load_yaml(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ValueError(f"YAML at {path} is not a mapping")
        return data


def _find_vars_file(project_root: Path) -> Optional[Path]:
    """
    Locate a sensible vars.yml. Priority:
      1) env SPINE_VARS_FILE
      2) config/spine/pipelines/local/vars.yml
      3) config/spine/pipelines/dev/vars.yml
      4) config/spine/pipelines/default/vars.yml
      5) config/spine/vars.yml
    """
    env_path = os.getenv("SPINE_VARS_FILE")
    if env_path:
        p = Path(env_path).expanduser().resolve()
        if p.exists():
            return p

    candidates = [
        project_root / "config" / "spine" / "pipelines" / "local" / "vars.yml",
        project_root / "config" / "spine" / "pipelines" / "dev" / "vars.yml",
        project_root / "config" / "spine" / "pipelines" / "default" / "vars.yml",
        project_root / "config" / "spine" / "vars.yml",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _find_caps_file(project_root: Path) -> Optional[Path]:
    env_path = os.getenv("SPINE_CAPS_FILE")
    if env_path:
        p = Path(env_path).expanduser().resolve()
        if p.exists():
            return p
    p = project_root / "config" / "spine" / "capabilities.yml"
    return p if p.exists() else None


def _merge_payload(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (extra or {}).items():
        # Shallow merge is sufficient; nested dicts like ask_spec overwrite wholly
        out[k] = v
    return out


# ------------------------------- main ---------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    argv = argv or sys.argv[1:]

    project_root = _find_project_root(_THIS)
    # Root for the engine; keep both root and project_root identical in this runner
    root = project_root

    caps_file = _find_caps_file(project_root)
    if caps_file:
        print(f"[DOCSTRINGS] loaded capabilities from {caps_file}")

    # Required core keys to start; vars.yml will extend this
    payload: Dict[str, Any] = {
        "root": str(root),
        "project_root": str(project_root),
        # defaults; may be overridden by vars.yml
        "out_base": "output/patches_received",
        "out_file": str(project_root / "output" / "patches_received" / "engine.out.json"),
    }

    # Load vars.yml if present and merge into payload
    vars_file = _find_vars_file(project_root)
    if vars_file:
        try:
            vars_data = _load_yaml(vars_file)
            print(f"[DOCSTRINGS] merging vars from {vars_file}")
            payload = _merge_payload(payload, vars_data or {})
        except Exception as e:
            print(f"[DOCSTRINGS] WARNING: failed to load vars.yml ({vars_file}): {e}")

    # Minimal sanity: if out_file not an absolute path, anchor it to project_root
    out_file = Path(str(payload.get("out_file") or "")).expanduser()
    if not out_file.is_absolute():
        out_file = (project_root / out_file).resolve()
        payload["out_file"] = str(out_file)

    # Kick off the engine
    result = engine_run_v1(payload, context={"runner": "docstrings"})
    # Write a tiny sentinel next to out_file for CI/tools
    try:
        out_file.parent.mkdir(parents=True, exist_ok=True)
        (out_file.parent / "docstrings.ok").write_text("ok", encoding="utf-8")
    except Exception:
        pass

    # Compact print of the summary
    counts = result.get("counts", {}) if isinstance(result, dict) else {}
    print("\n=== Spine Run: Result ===")
    print("{")
    print(f'  "run_dir": "{result.get("run_dir", "")}",')
    print('  "counts": {')
    print(f'    "built_messages": {counts.get("built_messages", 0)},')
    print(f'    "built_batch": {counts.get("built_batch", 0)},')
    print(f'    "sanitized": {counts.get("sanitized", 0)},')
    print(f'    "verified": {counts.get("verified", 0)}')
    print("  }")
    print("}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


