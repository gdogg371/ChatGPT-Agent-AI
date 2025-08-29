# File: v2/backend/core/run/docstrings.py
"""
Back-compat runner that invokes the generic patch loop via Spine.

- Module can be executed as a script: `python -m v2.backend.core.run.docstrings`
- Loads vars.yml and config/llm.yml, then triggers the engine via Spine.
- Does NOT set environment variables; the LLM provider reads secrets directly
  from secret_management/secrets.yml|yaml under the project root.

All bundle work goes via Spine capabilities; we do not import code_bundles here.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import yaml  # PyYAML
except ImportError as e:  # pragma: no cover
    raise RuntimeError("PyYAML is required to run this CLI (`pip install pyyaml`)") from e

try:
    # Use the generic orchestrator to remain decoupled from Spine’s exact API
    from v2.backend.core.prompt_pipeline.executor.orchestrator import capability_run  # type: ignore
except Exception as e:  # pragma: no cover
    raise RuntimeError("run.docstrings requires executor.orchestrator.capability_run") from e


# ------------------------------- helpers -------------------------------------


def _read_yaml(p: Path) -> Dict[str, Any]:
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return {}
    return data


def _default_vars_path() -> Path:
    here = Path(__file__).resolve()
    core_dir = here.parent.parent  # v2/backend/core
    return core_dir / "config" / "spine" / "pipelines" / "default" / "vars.yml"


def _default_llm_cfg_path(project_root: Path) -> Path:
    return project_root / "config" / "llm.yml"


def _merge_dict(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(dst or {})
    for k, v in (src or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_dict(out[k], v)
        else:
            out[k] = v
    return out


def _apply_llm_profile(vars_map: Dict[str, Any], llm_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Merge provider/model/ask_spec from config/llm.yml into vars.
    Vars take precedence if already set explicitly.
    """
    out = dict(vars_map or {})
    out.setdefault("provider", llm_cfg.get("provider"))
    out.setdefault("model", llm_cfg.get("model"))
    if "ask_spec" in llm_cfg:
        out["ask_spec"] = _merge_dict(out.get("ask_spec", {}), llm_cfg["ask_spec"])
    return out


def _val(payload: Dict[str, Any], key: str, default: Any = None) -> Any:
    v = payload.get(key)
    return v if v is not None else default


# ------------------------------ public API ------------------------------------


def run_patch_loop(vars: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute a full patch loop using the general-purpose engine.
    Args:
        vars: mapping typically coming from config/spine/pipelines/default/vars.yml

    Returns:
        {"run_dir": "<abs path>", "counts": {...}}
    """
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

            # Introspection source
            "sqlalchemy_url": _val(vars, "sqlalchemy_url"),
            "sqlalchemy_table": _val(vars, "sqlalchemy_table"),
            "status": _val(vars, "status"),
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

    meta = getattr(arts[0], "meta", arts[0]) if arts else {}
    if isinstance(meta, dict) and ("run_dir" in meta or "counts" in meta):
        return meta

    return {
        "run_dir": (meta.get("run_dir") if isinstance(meta, dict) else None),
        "counts": (meta.get("counts") if isinstance(meta, dict) else {}),
    }


# ------------------------------- CLI main -------------------------------------


def _main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the generic patch loop (docstrings back-compat entry).")
    parser.add_argument("--project-root", type=str, default=None, help="Project root (for config discovery). Default: current working dir.")
    parser.add_argument("--vars", type=str, default=None, help="Path to vars.yml. Default: v2/backend/core/config/spine/pipelines/default/vars.yml")
    parser.add_argument("--provider", type=str, default=None, help='LLM provider override (e.g., "openai")')
    parser.add_argument("--model", type=str, default=None, help="Model name override")
    parser.add_argument("--status", type=str, default=None, help="Filter status for introspection source")
    parser.add_argument("--max-rows", type=int, default=None, help="Row limit for introspection source")
    args = parser.parse_args(argv)

    project_root = Path(args.project_root or os.getcwd()).resolve()
    vars_path = Path(args.vars).resolve() if args.vars else _default_vars_path()

    vars_map = _read_yaml(vars_path)
    llm_cfg = _read_yaml(_default_llm_cfg_path(project_root))
    vars_map = _apply_llm_profile(vars_map, llm_cfg)

    # Apply CLI overrides (if any)
    if args.provider:
        vars_map["provider"] = args.provider
    if args.model:
        vars_map["model"] = args.model
    if args.status:
        vars_map["status"] = args.status
    if args.max_rows is not None:
        vars_map["max_rows"] = args.max_rows

    result = run_patch_loop(vars_map)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())

