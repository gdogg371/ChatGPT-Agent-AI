#v2\backend\core\run\docstrings.py
"""
Docstrings runner (YAML-only, fail-fast, platform-agnostic).

- Loads all configuration strictly via the centralized loader:
    * Spine      → capabilities path and pipeline profile
    * DB         → SQLAlchemy URL (SQLite path is normalized to URL by loader)
    * LLM        → provider/model (non-secret)
    * Packager   → include/exclude globs and segment-level excludes
    * PipelineVars (per profile) → stage toggles, table, status, limits, out_base, ask_spec
- Additionally reads `docstrings_model_path` from vars.yml (required for the scanner step).
- Passes variables to the Spine pipeline without any inline defaults.
- Fails early if any YAML is missing or malformed.

Invoke:
    python -m v2.backend.core.run.docstrings
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

try:
    import yaml  # type: ignore
except Exception as e:  # pragma: no cover
    raise RuntimeError("Required dependency 'PyYAML' is not installed.") from e

from v2.backend.core.configuration.loader import (
    ConfigError,
    get_repo_root,
    get_config_root,
    get_spine,
    get_db,
    get_llm,
    get_packager,
    get_pipeline_vars,
)
from v2.backend.core.spine import Spine, to_dict


def _ensure_sqlite_dir(sqlalchemy_url: str) -> None:
    """
    If using a sqlite URL, ensure the parent directory exists so the DB file
    can be created by SQLAlchemy on first use.
    """
    prefix = "sqlite:///"
    if isinstance(sqlalchemy_url, str) and sqlalchemy_url.startswith(prefix):
        db_path = sqlalchemy_url[len(prefix) :]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)


def _summarize_result(uri: str, meta: Dict[str, Any]) -> str:
    """
    Short human-readable trailer for artifact listing.
    """
    res = meta.get("result")
    if res is None:
        return ""
    if isinstance(res, dict):
        if "unpacked" in res:
            try:
                return f"(unpacked={int(res.get('unpacked') or 0)})"
            except Exception:
                return ""
        raw = res.get("raw")
        if isinstance(raw, list):
            return f"(llm_responses={len(raw)})"
    return ""


def _print_artifacts(arts: List[Any]) -> None:
    print("\n=== Spine Run: Artifacts ===")
    if not arts:
        print("(none)")
        return
    problems: List[Dict[str, Any]] = []
    for idx, a in enumerate(arts, 1):
        d = to_dict(a)
        kind = d.get("kind")
        uri = d.get("uri")
        extra = ""
        if kind == "Result":
            extra = " " + _summarize_result(uri or "", d.get("meta") or {})
        print(f"{idx:02d}. {kind:7} {uri}{extra}")
        if kind == "Problem":
            pr = (d.get("meta") or {}).get("problem") or {}
            problems.append(pr)
    if problems:
        print("\nProblems:")
        for pr in problems:
            code = pr.get("code", "Unknown")
            msg = pr.get("message", "")
            print(f" - {code}: {msg}")


def _read_vars_key(profile: str, key: str) -> Any:
    """
    Read a single key from config\\spine\\pipelines\\<profile>\\vars.yml.
    Fail-fast if the file or key is missing.
    """
    vars_path = get_config_root() / "spine" / "pipelines" / profile / "vars.yml"
    if not vars_path.is_file():
        raise ConfigError(f"Missing vars.yml for profile '{profile}': {vars_path}")
    try:
        with vars_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        raise ConfigError(f"Failed to parse YAML: {vars_path}: {e}") from e
    if not isinstance(data, dict) or key not in data:
        raise ConfigError(f"{vars_path}: missing required key '{key}'")
    return data[key]


def _collect_variables_from_yaml() -> dict:
    """
    Load config from loader and emit the variables the pipeline expects,
    including provider/model/sqlalchemy_url.
    """
    from v2.backend.core.configuration.loader import (
        get_pipeline_vars,
        get_llm,
        get_db,
    )

    pv = get_pipeline_vars()
    llm = get_llm()          # must provide .provider and .model
    db  = get_db()           # must provide .sqlalchemy_url (or .url/.dsn fallback)

    sqlalchemy_url = (
        getattr(db, "sqlalchemy_url", None)
        or getattr(db, "url", None)
        or getattr(db, "dsn", None)
        or ""
    )
    if not sqlalchemy_url:
        raise RuntimeError("DB config missing: sqlalchemy_url/url/dsn not set")

    return {
        # --- LLM required by the pipeline ---
        "provider": getattr(llm, "provider", "").strip(),
        "model": getattr(llm, "model", "").strip(),

        # --- DB / filters ---
        "sqlalchemy_url": sqlalchemy_url,
        "sqlalchemy_table": getattr(pv, "sqlalchemy_table", "introspection_index"),
        "status_filter": getattr(pv, "status_filter", "") or "",
        "max_rows": int(getattr(pv, "max_rows", 200) or 200),

        # --- outputs ---
        "out_base": getattr(pv, "out_base", "") or "",
        "exclude_globs": list(getattr(pv, "exclude_globs", []) or []),
        "segment_excludes": list(getattr(pv, "segment_excludes", []) or []),

        # --- LLM/Pipeline extras ---
        "ask_spec": dict(getattr(pv, "ask_spec", {}) or {}),
        "docstrings_model_path": getattr(pv, "docstrings_model_path", "") or "",

        # --- canonical engine toggles ---
        "run_fetch_targets": bool(getattr(pv, "run_fetch_targets", True)),
        "run_build_prompts": bool(getattr(pv, "run_build_prompts", True)),
        "run_run_llm": bool(getattr(pv, "run_run_llm", True)),
        "run_unpack": bool(getattr(pv, "run_unpack", True)),
        "run_sanitize": bool(getattr(pv, "run_sanitize", True)),
        "run_verify": bool(getattr(pv, "run_verify", True)),
        "run_save_patch": bool(getattr(pv, "run_save_patch", True)),
        "run_apply_patch_sandbox": bool(getattr(pv, "run_apply_patch_sandbox", False)),
        "run_archive_and_replace": bool(getattr(pv, "run_archive_and_replace", False)),
        "run_rollback": bool(getattr(pv, "run_rollback", False)),
    }



def main() -> int:
    try:
        spine_cfg = get_spine()
    except ConfigError as e:
        print(f"[config] {e}", file=sys.stderr)
        return 2

    caps_path = spine_cfg.caps_path
    pipeline_path = spine_cfg.pipelines_root / spine_cfg.profile / "patch_loop.yml"

    if not caps_path.is_file():
        print(f"ERROR: capabilities file not found: {caps_path}", file=sys.stderr)
        return 2
    if not pipeline_path.is_file():
        print(f"ERROR: pipeline file not found: {pipeline_path}", file=sys.stderr)
        return 2

    variables = _collect_variables_from_yaml()
    spine_rt = Spine(caps_path=caps_path)
    artifacts = spine_rt.load_pipeline_and_run(pipeline_path, variables=variables)

    # Pretty-print any Result/Problem meta so you can see counts & errors
    for i, a in enumerate(artifacts, 1):
        kind = getattr(a, "kind", "")
        meta = getattr(a, "meta", {}) or {}
        print(f"\n--- Artifact {i}: {kind} ---")
        try:
            import json
            print(json.dumps(meta, indent=2))
        except Exception:
            print(meta)

    _print_artifacts(artifacts)

    # Non-zero exit if any Problem artifact is present
    exit_code = 0
    for a in artifacts:
        if getattr(a, "kind", None) == "Problem" or to_dict(a).get("kind") == "Problem":
            exit_code = 1
            break
    return exit_code


if __name__ == "__main__":
    sys.exit(main())







