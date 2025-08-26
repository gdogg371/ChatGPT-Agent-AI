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


def _collect_variables_from_yaml() -> Dict[str, Any]:
    """
    Build the pipeline variables purely from YAML configs via the loader
    (plus one explicit read from vars.yml for docstrings_model_path).
    No defaults or env fallbacks are introduced here.
    """
    repo_root = get_repo_root()
    spine = get_spine()
    db = get_db()
    llm = get_llm()
    pack = get_packager()
    pv = get_pipeline_vars(spine.profile)

    # Exclude patterns (single source of truth)
    exclude_globs = list(pack.exclude_globs)
    segment_excludes = list(pack.segment_excludes)

    # Ensure SQLite directory exists if applicable
    sqlalchemy_url = db.url
    _ensure_sqlite_dir(sqlalchemy_url)

    # Required for scanner step (validated here)
    docstrings_model_path = _read_vars_key(spine.profile, "docstrings_model_path")
    if not isinstance(docstrings_model_path, str) or not docstrings_model_path.strip():
        raise ConfigError("docstrings_model_path must be a non-empty string")

    # Variables consumed by config\\spine\\pipelines\\<profile>\\patch_loop.yml
    variables: Dict[str, Any] = {
        # Engine payload scalars
        "provider": llm.provider,
        "model": llm.model,
        "max_rows": pv.max_rows,
        "sqlalchemy_url": sqlalchemy_url,
        "sqlalchemy_table": pv.sqlalchemy_table,
        "status_filter": pv.status_filter,
        "exclude_globs": exclude_globs,
        "segment_excludes": segment_excludes,
        "out_base": pv.out_base,
        "ask_spec": dict(pv.ask_spec or {}),

        # Scanner-only
        "docstrings_model_path": docstrings_model_path,

        # Legacy-style RUN_* flags referenced by the pipeline
        "RUN_FETCH": bool(pv.RUN_FETCH),
        "RUN_BUILD": bool(pv.RUN_BUILD),
        "RUN_LLM": bool(pv.RUN_LLM),
        "RUN_UNPACK": bool(pv.RUN_UNPACK),
        "RUN_SANITIZE": bool(pv.RUN_SANITIZE),
        "RUN_VERIFY": bool(pv.RUN_VERIFY),
        "RUN_WRITE": bool(pv.RUN_WRITE),
        "RUN_APPLY": bool(pv.RUN_APPLY),
        "RUN_ARCHIVE": bool(pv.RUN_ARCHIVE),
        "RUN_ROLLBACK": bool(pv.RUN_ROLLBACK),

        # Project roots (available to steps if needed)
        "PROJECT_ROOT": str(repo_root),
        "SCAN_ROOT": str(repo_root),
    }

    # Minimal echo for visibility
    print("[vars] DB_URL =", sqlalchemy_url)
    print("[vars] TABLE  =", pv.sqlalchemy_table)
    return variables


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







