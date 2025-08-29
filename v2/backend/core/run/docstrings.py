# File: v2/backend/core/run/docstrings.py
"""
Back-compat runner that invokes the generic patch loop via Spine.

- Execute as: `python -m v2.backend.core.run.docstrings`
- Vars:   <project_root>/config/spine/pipelines/default/vars.yml
- LLM:    <project_root>/config/llm.yml
- Caps:   <project_root>/config/spine/capabilities.yml
- No env vars. No directory walking. All paths are fixed relative to project_root.

Now also surfaces any Problem artifacts returned by capabilities so we can see
why the engine bailed (e.g., fetch failures), instead of hiding them.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse

try:
    import yaml  # PyYAML
except ImportError as e:  # pragma: no cover
    raise RuntimeError("PyYAML is required to run this CLI (`pip install pyyaml`)") from e

# Orchestrator + Spine loader
from v2.backend.core.prompt_pipeline.executor.orchestrator import capability_run  # type: ignore
from v2.backend.core.spine.loader import CapabilitiesLoader, get_registry  # type: ignore


# ------------------------------- fixed paths ----------------------------------


def _vars_path(project_root: Path) -> Path:
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


# ----------------------------- artifact helpers -------------------------------


def _unwrap_first_meta(arts: Any) -> Dict[str, Any]:
    """
    Returns the first artifact's meta or {}.
    Accepts list[Artifact|dict] or dict.
    """
    if not arts:
        return {}
    if isinstance(arts, list) and arts:
        first = arts[0]
        if isinstance(first, dict):
            return first.get("meta", first)
        meta = getattr(first, "meta", None)
        return meta if isinstance(meta, dict) else {}
    if isinstance(arts, dict):
        return arts
    return {}


def _collect_problems(arts: Any) -> List[Dict[str, Any]]:
    """Collect any Problem artifacts (kind == 'Problem' or meta.problem present)."""
    problems: List[Dict[str, Any]] = []

    def _as_dict(a: Any) -> Dict[str, Any]:
        if isinstance(a, dict):
            return a
        # Attempt to read Artifact-like
        return {
            "kind": getattr(a, "kind", None),
            "uri": getattr(a, "uri", None),
            "meta": getattr(a, "meta", None),
        }

    if isinstance(arts, list):
        for a in arts:
            d = _as_dict(a)
            kind = d.get("kind")
            meta = d.get("meta") or {}
            if kind == "Problem" or ("problem" in meta) or ("error" in meta and "message" in meta):
                problems.append(
                    {
                        "kind": kind,
                        "uri": d.get("uri"),
                        "problem": meta.get("problem"),
                        "error": meta.get("error"),
                        "message": meta.get("message"),
                        "exception": meta.get("exception"),
                        "traceback": meta.get("traceback"),
                        "meta": {k: v for k, v in meta.items() if k not in {"problem", "error", "message", "exception", "traceback"}},
                    }
                )
    elif isinstance(arts, dict):
        d = arts
        if d.get("kind") == "Problem" or "problem" in d or "error" in d:
            problems.append(d)
    return problems


# ------------------------------ safety guard ----------------------------------


def _assert_sqlite_file_exists(sqlalchemy_url: str, project_root: Path) -> None:
    if not sqlalchemy_url or not sqlalchemy_url.startswith("sqlite"):
        return
    parsed = urlparse(sqlalchemy_url)
    path = unquote(parsed.path or "")

    if sqlalchemy_url.startswith("sqlite:////"):
        fs = path[1:] if len(path) >= 3 and path[0] == "/" and path[2] == ":" else path
        p = Path(fs).resolve()
        if not p.exists():
            raise FileNotFoundError(
                f"SQLite database file not found at absolute path: {p} "
                "(Hint: for project-relative DB use sqlite:///databases/your.db)"
            )
        return

    if sqlalchemy_url.startswith("sqlite:///"):
        if len(path) >= 3 and path[0] == "/" and path[2] == ":":
            fs = path[1:]
            p = Path(fs).resolve()
        else:
            rel = path.lstrip("/")
            p = (project_root / rel).resolve()
        if not p.exists():
            raise FileNotFoundError(
                f"SQLite database file not found: {p} "
                "(Check the path; relative URLs use three slashes: sqlite:///databases/your.db)"
            )


# -------------------------- capability bootstrapping --------------------------


def _ensure_capabilities_loaded(project_root: Path) -> None:
    caps = _caps_path(project_root)
    if not caps.exists():
        raise FileNotFoundError(f"Spine capabilities map not found: {caps}")
    CapabilitiesLoader(caps).load(get_registry())


# ------------------------------- main routine ---------------------------------


def run_patch_loop(vars: Dict[str, Any], project_root: Path) -> Dict[str, Any]:
    _ensure_capabilities_loaded(project_root)

    url = vars.get("sqlalchemy_url")
    if url:
        _assert_sqlite_file_exists(url, project_root)

    # BUNDLE SNAPSHOT
    capability_run(
        "packager.bundle.make.v1",
        {
            "root": _val(vars, "patch_target_root", "."),
            "project_root": _val(vars, "patch_target_root", "."),
            "out_base": _val(vars, "out_base", "output/patches_received"),
        },
        {"phase": "BUNDLE.MAKE", "runner": "run.docstrings"},
    )

    # ENGINE
    arts = capability_run(
        "llm.engine.run.v1",
        {
            # LLM
            "provider": _val(vars, "provider", "openai"),
            "model": _val(vars, "model", ""),
            "ask_spec": _val(vars, "ask_spec", {}),
            # Introspection
            "sqlalchemy_url": _val(vars, "sqlalchemy_url"),
            "sqlalchemy_table": _val(vars, "sqlalchemy_table"),
            "status": _val(vars, "status", _val(vars, "status_filter", None)),
            "status_any": _val(vars, "status_any"),
            "max_rows": _val(vars, "max_rows", 50),
            # Filters
            "exclude_globs": _val(vars, "exclude_globs", []),
            "segment_excludes": _val(vars, "segment_excludes", []),
            # Toggles
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
            # Patch options
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

    meta = _unwrap_first_meta(arts)
    result = meta.get("result") or meta
    run_dir = result.get("run_dir")
    counts = result.get("counts")

    problems = _collect_problems(arts)
    return {
        "run_dir": str(run_dir) if run_dir else None,
        "counts": counts if isinstance(counts, dict) else {},
        "problems": problems,  # <— see why FETCH bailed
    }


# ----------------------------------- CLI --------------------------------------


def _main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the generic patch loop (docstrings back-compat entry).")
    parser.add_argument("--project-root", type=str, default=None, help="Project root. Default: current working dir.")
    args = parser.parse_args(argv)

    project_root = Path(args.project_root or os.getcwd()).resolve()
    vars_file = _vars_path(project_root)
    llm_file = _llm_cfg_path(project_root)
    caps_file = _caps_path(project_root)

    if not caps_file.exists():
        raise FileNotFoundError(f"Spine capabilities map not found: {caps_file}")
    vars_map = _read_yaml_required(vars_file)
    llm_cfg = _read_yaml_optional(llm_file)
    vars_map = _apply_llm_profile(vars_map, llm_cfg)

    result = run_patch_loop(vars_map, project_root)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())




