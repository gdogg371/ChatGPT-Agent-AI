# File: backend/core/prompt_pipeline/executor/engine.py
from __future__ import annotations

"""
Spine-only façade for the LLM patch loop.

This module replaces the previous monolithic Engine class that directly imported
DB, LLM, and patching code. It now serves as a **provider** entrypoint for the
Spine capability `llm.engine.run.v1`.

Responsibilities:
- pick a pipeline YAML (explicit path → per-profile → default)
- pass through a variables dict to the Spine pipeline runner
- return the resulting List[Artifact]

No cross-domain imports: all work is delegated to Spine capabilities.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

# NOTE: Use the actual package root ("backend"), not "v2.backend".
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
    Prepare a variables dict for the pipeline.
    We mirror common names to uppercase so YAML can refer to them as ${VAR}.
    """
    vars_in: Dict[str, Any] = dict(payload)

    # Mirror a few well-known keys with conventional uppercase aliases
    mirror_keys = {
        "provider": "PROVIDER",
        "model": "MODEL",
        "api_key": "API_KEY",
        "sqlalchemy_url": "DB_URL",
        "sqlalchemy_table": "TABLE",
        "status_filter": "STATUS",
        "max_rows": "MAX_ROWS",
        "project_root": "PROJECT_ROOT",
        "out_base": "OUT_BASE",
        "scan_root": "SCAN_ROOT",
        "scan_exclude_globs": "EXCLUDES",
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

    # Ensure some path-y things are strings (YAML substitution-friendly)
    for key in ("PROJECT_ROOT", "OUT_BASE", "SCAN_ROOT"):
        v = vars_in.get(key)
        if isinstance(v, (str, Path)):
            vars_in[key] = str(v)

    # Ensure EXCLUDES is a list
    if "EXCLUDES" in vars_in and not isinstance(vars_in["EXCLUDES"], (list, tuple)):
        ex = vars_in["EXCLUDES"]
        vars_in["EXCLUDES"] = [ex] if isinstance(ex, str) else list(ex or [])

    # Pass-through ask_spec if provided
    if "ask_spec" in payload and "ASK_SPEC" not in vars_in:
        vars_in["ASK_SPEC"] = payload["ask_spec"]

    return vars_in


# ------------------------------ provider entrypoint -------------------------


def run_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    """
    Provider for capability: llm.engine.run.v1

    Expected payload shape (dict). Typical keys include:
      - provider, model, api_key
      - sqlalchemy_url, sqlalchemy_table, status_filter, max_rows
      - project_root, out_base, scan_root, scan_exclude_globs
      - confirm_prod_writes, preserve_crlf, verbose
      - ask_spec (dict)
      - (optional) pipeline_yaml (explicit path)
      - (optional) spine_profile (override profile folder)

    Returns List[Artifact] from the final pipeline step.
    """
    payload = task.payload or {}
    if not isinstance(payload, dict):
        raise TypeError("llm.engine.run.v1 expects a dict payload")

    # Resolve pipeline file and variables
    pipeline_yaml = _select_pipeline_yaml(payload)
    variables = _build_variables(payload)

    # Build spine and run
    spine = Spine(caps_path=SPINE_CAPS_PATH)
    artifacts = spine.load_pipeline_and_run(pipeline_yaml, variables=variables)
    return artifacts


# ------------------------------ static self-test ----------------------------

if __name__ == "__main__":
    """
    Minimal static test that exercises path resolution without executing the pipeline.
    Exits 0 if SPINE paths resolve and the selected pipeline file exists; non-zero otherwise.
    """

    ok, msg = sanity_check()
    print("[engine] spine_paths sanity:", msg)

    # Build a dummy payload to test selection behavior
    payload = {
        "spine_profile": SPINE_PROFILE,
        # no explicit "pipeline_yaml" → selection falls back to profile/default
    }

    pipeline_path = _select_pipeline_yaml(payload)
    print(f"[engine] selected pipeline: {pipeline_path}")

    # Summarize outcome; we don't execute the pipeline in this static test
    exists = pipeline_path.exists()
    print(f"[engine] pipeline exists: {exists}")
    print(f"[engine] pipelines root: {SPINE_PIPELINES_ROOT}")

    raise SystemExit(0 if (ok and exists) else 2)



