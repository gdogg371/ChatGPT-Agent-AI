# File: v2/backend/core/run/docstrings.py
"""
Back-compat runner that invokes the generic patch loop via Spine.

- Execute as: `python -m v2.backend.core.run.docstrings`
- Vars are read from: <project_root>/config/spine/pipelines/default/vars.yml
- LLM config is read from: <project_root>/config/llm.yml
- Capabilities are loaded from: <project_root>/config/spine/capabilities.yml
- No env vars. No directory walking. All paths are fixed relative to project_root.

This file stays domain-agnostic. It wires inputs and calls Spine capabilities.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import unquote, urlparse

try:
    import yaml  # PyYAML
except ImportError as e:  # pragma: no cover
    raise RuntimeError("PyYAML is required to run this CLI (`pip install pyyaml`)") from e

# Generic orchestrator
from v2.backend.core.prompt_pipeline.executor.orchestrator import capability_run  # type: ignore
# Spine loader (to explicitly load capabilities before running)
from v2.backend.core.spine.loader import CapabilitiesLoader, get_registry  # type: ignore


# ------------------------------- fixed paths ----------------------------------


def _vars_path(project_root: Path) -> Path:
    """Fixed location for pipeline vars. No env. No walking."""
    return (project_root / "config" / "spine" / "pipelines" / "default" / "vars.yml").resolve()


def _llm_cfg_path(project_root: Path) -> Path:
    return (project_root / "config" / "llm.yml").resolve()


def _caps_path(project_root: Path) -> Path:
    return (project_root / "config" / "spine" / "capabilities.yml").resolve()


# --------------------------------- io utils -----------------------------------


def _read_yaml_required(p: Path) -> Dict[str, Any]:
    if not p.exists():
        raise FileNotFoundError(f"Required YAML not found: {p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be a mapping: {p}")
    return data


def _read_yaml_optional(p: Path) -> Dict[str, Any]:
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def _merge_dict(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(dst or {})
    for k, v in (src or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_dict(out[k], v)
        else:
            out[k] = v
    return out


def _apply_llm_profile(vars_map: Dict[str, Any], llm_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Merge provider/model/ask_spec from config/llm.yml into vars (vars override)."""
    out = dict(vars_map or {})
    if "provider" not in out and "provider" in llm_cfg:
        out["provider"] = llm_cfg["provider"]
    if "model" not in out and "model" in llm_cfg:
        out["model"] = llm_cfg["model"]
    if "ask_spec" in llm_cfg:
        out["ask_spec"] = _merge_dict(out.get("ask_spec", {}), llm_cfg["ask_spec"])
    return out


def _val(payload: Dict[str, Any], key: str, default: Any = None) -> Any:
    v = payload.get(key)
    return v if v is not None else default


def _unwrap_meta(maybe_art_list: Any) -> Dict[str, Any]:
    """
    Accepts:
      - list[Artifact]  (Artifact has `.meta`)
      - list[dict]      (each dict may be {"meta": {...}})
      - dict            (direct result)
    Returns a dict (meta/result dict) or {}.
    """
    if maybe_art_list is None:
        return {}
    if isinstance(maybe_art_list, list) and maybe_art_list:
        first = maybe_art_list[0]
        meta = getattr(first, "meta", None) if not isinstance(first, dict) else first.get("meta")
        if isinstance(meta, dict):
            return meta.get("result") or meta  # unwrap `result` if present
        if isinstance(first, dict):
            return first
        return {}
    if isinstance(maybe_art_list, dict):
        return maybe_art_list
    return {}


# ------------------------------ safety guard ----------------------------------


def _assert_sqlite_file_exists(sqlalchemy_url: str, project_root: Path) -> None:
    """
    Fail fast if a sqlite URL points to a non-existent file (common 3 vs 4 slash mistakes).

    Rules:
      - sqlite:///databases/bot_dev.db     -> project-root relative
      - sqlite:///C:/Users/.../db.sqlite   -> absolute Windows (strip leading slash)
      - sqlite:////databases/bot_dev.db    -> absolute POSIX root (usually wrong on Windows)
    """
    if not sqlalchemy_url or not sqlalchemy_url.startswith("sqlite"):
        return  # non-sqlite URLs are out of scope for this guard

    parsed = urlparse(sqlalchemy_url)
    path = unquote(parsed.path or "")

    if sqlalchemy_url.startswith("sqlite:////"):
        # Absolute path. Normalize Windows drive-style (/C:/...) by stripping leading slash.
        fs = path[1:] if len(path) >= 3 and path[0] == "/" and path[2] == ":" else path
        p = Path(fs).resolve()
        if not p.exists():
            raise FileNotFoundError(
                f"SQLite database file not found at absolute path: {p} "
                "(Hint: for project-relative DB use sqlite:///databases/your.db)"
            )
        return

    if sqlalchemy_url.startswith("sqlite:///"):
        # Triple slash: treat as project-root relative unless it's '/C:/...'
        if len(path) >= 3 and path[0] == "/" and path[2] == ":":
            # '/C:/Users/...'
            fs = path[1:]
            p = Path(fs).resolve()
        else:
            rel = path.lstrip("/")  # '/databases/bot_dev.db' -> 'databases/bot_dev.db'
            p = (project_root / rel).resolve()
        if not p.exists():
            raise FileNotFoundError(
                f"SQLite database file not found: {p} "
                "(Check the path and remember: relative URLs use three slashes: sqlite:///databases/your.db)"
            )
        return
    # Other sqlite schemes (e.g., memory) are ignored.


# -------------------------- capability bootstrapping --------------------------


def _ensure_capabilities_loaded(project_root: Path) -> None:
    """Load <project_root>/config/spine/capabilities.yml into the registry."""
    caps = _caps_path(project_root)
    if not caps.exists():
        raise FileNotFoundError(f"Spine capabilities map not found: {caps}")
    CapabilitiesLoader(caps).load(get_registry())


# ------------------------------- main routine ---------------------------------


def run_patch_loop(vars: Dict[str, Any], project_root: Path) -> Dict[str, Any]:
    """
    Execute a full patch loop using the general-purpose engine.

    Returns:
      {"run_dir": "<abs path>|None", "counts": {...}}
    """
    # Ensure the registry is populated
    _ensure_capabilities_loaded(project_root)

    # Validate the sqlite URL early so a bad path doesn't "silently succeed"
    url = vars.get("sqlalchemy_url")
    if url:
        _assert_sqlite_file_exists(url, project_root)

    # 0) Ensure code-bundle snapshot (Spine → code_bundles, no direct imports)
    capability_run(
        "packager.bundle.make.v1",
        {
            "root": _val(vars, "patch_target_root", "."),
            "project_root": _val(vars, "patch_target_root", "."),
            "out_base": _val(vars, "out_base", "output/patches_received"),
        },
        {"phase": "BUNDLE.MAKE", "runner": "run.docstrings"},
    )

    # 1) Generic engine run
    arts = capability_run(
        "llm.engine.run.v1",
        {
            # LLM
            "provider": _val(vars, "provider", "openai"),
            "model": _val(vars, "model", ""),
            "ask_spec": _val(vars, "ask_spec", {}),
            # Introspection source (MUST be provided by vars.yml)
            "sqlalchemy_url": _val(vars, "sqlalchemy_url"),
            "sqlalchemy_table": _val(vars, "sqlalchemy_table"),
            # The fetch provider accepts: status | status_any
            "status": _val(vars, "status", _val(vars, "status_filter", None)),
            "status_any": _val(vars, "status_any"),
            "max_rows": _val(vars, "max_rows", 50),
            # Filters
            "exclude_globs": _val(vars, "exclude_globs", []),
            "segment_excludes": _val(vars, "segment_excludes", []),
            # Stage toggles
            "run_fetch_targets": _val(vars, "run_fetch_targets", True),
            "run_build_prompts": _val(vars, "run_build_prompts", True),
            "run_run_llm": _val(vars, "run_run_llm", True),
            "run_unpack": _val(vars, "run_unpack", True),
            "run_sanitize": _val(vars, "run_sanitize", True),
            "run_verify": _val(vars, "run_verify", True),
            "run_save_patch": _val(vars, "run_save_patch", True),
            "run_apply_patch_sandbox": _val(vars, "run_apply_patch_sandbox", False),
            "run_archive_and_replace": _val(vars, "run_archive_and_replace", False),
            "run_rollback": _val(vars, "run_rollback", False),
            # Patch application options
            "patch_target_root": _val(vars, "patch_target_root", "."),
            "patch_seed_strategy": _val(vars, "patch_seed_strategy", "once"),
            "strip_prefix": _val(vars, "strip_prefix", ""),
            "mirror_to": _val(vars, "patch_target_root", "."),
            # Outputs
            "out_base": _val(vars, "out_base", "output/patches_received"),
            "out_file": _val(vars, "out_file", "output/patches_received/summary.json"),
        },
        {"phase": "ENGINE", "runner": "run.docstrings"},
    )

    # Unwrap the engine’s summary robustly; always return deterministic keys
    meta = _unwrap_meta(arts)
    result = meta.get("result") or meta  # prefer inner result if present
    run_dir = result.get("run_dir")
    counts = result.get("counts")

    return {
        "run_dir": str(run_dir) if run_dir else None,
        "counts": counts if isinstance(counts, dict) else {},
    }


# ----------------------------------- CLI --------------------------------------


def _main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the generic patch loop (docstrings back-compat entry)."
    )
    parser.add_argument(
        "--project-root",
        type=str,
        default=None,
        help="Project root. Default: current working dir.",
    )
    # No --vars: file location is fixed relative to project_root
    args = parser.parse_args(argv)

    project_root = Path(args.project_root or os.getcwd()).resolve()

    vars_file = _vars_path(project_root)
    llm_file = _llm_cfg_path(project_root)
    caps_file = _caps_path(project_root)

    # Fail early if essentials are missing
    if not caps_file.exists():
        raise FileNotFoundError(f"Spine capabilities map not found: {caps_file}")
    vars_map = _read_yaml_required(vars_file)
    llm_cfg = _read_yaml_optional(llm_file)
    vars_map = _apply_llm_profile(vars_map, llm_cfg)

    # Run the loop
    result = run_patch_loop(vars_map, project_root)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())



