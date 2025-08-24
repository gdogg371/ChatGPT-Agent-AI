# File: v2/backend/core/prompt_pipeline/executor/engine.py
from __future__ import annotations

"""
Spine-only façade for the LLM patch loop.

Provider: llm.engine.run.v1

Responsibilities:
- Pick a pipeline YAML (explicit path → per-profile → default)
- Build variables for the pipeline (including EXCLUDES when provided)
- Delegate execution to Spine and return the resulting artifacts
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from v2.backend.core.spine import Spine
from v2.backend.core.spine.contracts import Artifact, Task
from v2.backend.core.configuration.spine_paths import (
    SPINE_CAPS_PATH,
    SPINE_PIPELINES_ROOT,
    SPINE_PROFILE,
)

__all__ = ["run_v1"]


# ------------------------------ helpers -------------------------------------


def _as_path(p: Any) -> Optional[Path]:
    if p is None:
        return None
    try:
        return Path(str(p)).resolve()
    except Exception:
        return None


def _select_pipeline_yaml(payload: Dict[str, Any]) -> Path:
    """
    Choose the pipeline YAML to execute.

    Priority:
      1) payload["pipeline_yaml"] (absolute or project-relative)
      2) {SPINE_PIPELINES_ROOT}/{profile}/patch_loop.yml
      3) {SPINE_PIPELINES_ROOT}/default/patch_loop.yml
    """
    # 1) explicit path in payload
    p = payload.get("pipeline_yaml")
    if isinstance(p, (str, Path)):
        cand = _as_path(p)
        if cand and cand.exists():
            return cand

    root = SPINE_PIPELINES_ROOT
    profile = payload.get("spine_profile") or SPINE_PROFILE or "default"

    # 2) per-profile
    cand = root / str(profile) / "patch_loop.yml"
    if cand.is_file():
        return cand.resolve()

    # 3) default
    fallback = root / "default" / "patch_loop.yml"
    return fallback.resolve()


def _build_variables(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Prepare variables for the pipeline. We mirror common keys to uppercase so
    YAML can refer to them via ${VAR}. We also honor 'exclude_globs' (preferred)
    or 'scan_exclude_globs' (legacy). If neither is present, we DO NOT inject
    EXCLUDES so that pipeline YAML defaults apply.
    """
    vars_in: Dict[str, Any] = dict(payload)

    # Prefer 'exclude_globs'; fall back to 'scan_exclude_globs'
    excludes_val = payload.get("exclude_globs")
    if not excludes_val:
        excludes_val = payload.get("scan_exclude_globs")

    # Optionally pass baseline directory exclusions (basenames, not globs)
    exclude_dirs_val = payload.get("exclude_dirs")

    # Mirror a few well-known keys with conventional uppercase aliases
    mirror_keys = {
        "provider": "PROVIDER",
        "model": "MODEL",
        "api_key": "API_KEY",
        "sqlalchemy_url": "DB_URL",
        "db_url": "DB_URL",            # alias accepted
        "sqlalchemy_table": "TABLE",
        "table": "TABLE",              # alias accepted
        "status_filter": "STATUS",
        "status": "STATUS",            # alias accepted
        "max_rows": "MAX_ROWS",
        "project_root": "PROJECT_ROOT",
        "out_base": "OUT_BASE",
        "scan_root": "SCAN_ROOT",
        # EXCLUDES handled explicitly below (to avoid overriding defaults)
        "confirm_prod_writes": "CONFIRM",
        "preserve_crlf": "PRESERVE_CRLF",
        "model_ctx_tokens": "MODEL_CTX",
        "response_tokens_per_item": "RESP_TOKENS_PER_ITEM",
        "batch_overhead_tokens": "BATCH_OVERHEAD_TOKENS",
        "budget_guardrail": "BUDGET_GUARDRAIL",
    }
    for k, up in mirror_keys.items():
        if k in vars_in and up not in vars_in:
            vars_in[up] = vars_in[k]

    # Ensure path-like values are strings (YAML substitution-friendly)
    for key in ("PROJECT_ROOT", "OUT_BASE", "SCAN_ROOT"):
        v = vars_in.get(key)
        if isinstance(v, (str, Path)):
            vars_in[key] = str(v)

    # Normalize EXCLUDES → list[str] ONLY if provided in payload.
    # Otherwise, leave it unset so the YAML default can take effect.
    if excludes_val is not None:
        if isinstance(excludes_val, (list, tuple)):
            vars_in["EXCLUDES"] = [str(x) for x in excludes_val]
        elif isinstance(excludes_val, str):
            vars_in["EXCLUDES"] = [excludes_val]
        else:
            # Unknown shape → safest is to omit and let YAML defaults apply
            pass

    # Normalize EXCLUDE_DIRS (directory basenames)
    if exclude_dirs_val is not None:
        if isinstance(exclude_dirs_val, (list, tuple)):
            vars_in["EXCLUDE_DIRS"] = [str(x) for x in exclude_dirs_val if x]
        elif isinstance(exclude_dirs_val, str):
            vars_in["EXCLUDE_DIRS"] = [exclude_dirs_val]
        # else: ignore bad types

    # Pass-through ask_spec if provided
    if "ask_spec" in payload and "ASK_SPEC" not in vars_in:
        vars_in["ASK_SPEC"] = payload["ask_spec"]

    return vars_in


def _sanity_check() -> Tuple[bool, str]:
    """
    Lightweight checks so running this module directly can fail fast
    without executing the full pipeline.
    """
    if not SPINE_CAPS_PATH or not Path(SPINE_CAPS_PATH).exists():
        return False, f"caps file missing: {SPINE_CAPS_PATH}"
    if not SPINE_PIPELINES_ROOT or not Path(SPINE_PIPELINES_ROOT).exists():
        return False, f"pipelines root missing: {SPINE_PIPELINES_ROOT}"
    # Ensure we can select a pipeline path
    p = _select_pipeline_yaml({})
    if not p.exists():
        return False, f"pipeline YAML not found: {p}"
    return True, f"ok ({p})"


# ------------------------------ provider entrypoint -------------------------


def run_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    """
    Provider for capability: llm.engine.run.v1
    """
    payload = task.payload or {}
    if not isinstance(payload, dict):
        raise TypeError("llm.engine.run.v1 expects a dict payload")

    pipeline_yaml = _select_pipeline_yaml(payload)
    if not pipeline_yaml.exists():
        raise FileNotFoundError(f"Pipeline YAML does not exist: {pipeline_yaml}")

    variables = _build_variables(payload)

    spine = Spine(caps_path=SPINE_CAPS_PATH)
    artifacts = spine.load_pipeline_and_run(pipeline_yaml, variables=variables)
    return artifacts


# ------------------------------ static self-test ----------------------------

if __name__ == "__main__":
    ok, msg = _sanity_check()
    print("[engine] spine_paths sanity:", msg)

    pipeline_path = _select_pipeline_yaml({})
    print(f"[engine] selected pipeline: {pipeline_path}")
    print(f"[engine] pipeline exists: {pipeline_path.exists()}")
    print(f"[engine] pipelines root: {SPINE_PIPELINES_ROOT}")

    raise SystemExit(0 if (ok and pipeline_path.exists()) else 2)

