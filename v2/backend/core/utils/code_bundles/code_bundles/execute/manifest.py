from __future__ import annotations

import inspect
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple
from types import SimpleNamespace as NS

# ---- SCANNERS (aligned to your monolith) ----
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.doc_coverage import (
    scan as scan_doc_coverage,
)
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.complexity import (
    scan as scan_complexity,
)
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.owners_index import (
    scan as scan_owners,
)
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.env_index import (
    scan as scan_env,
)
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.entrypoints import (
    scan as scan_entrypoints,
)
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.html_index import (
    scan as scan_html,
)
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.sql_index import (
    scan as scan_sql,
)
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.js_ts_index import (
    scan as scan_js_ts,
)
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.deps_scan import (
    scan_dependencies,
)
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.git_info import (
    scan as scan_git,
)
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.license_scan import (
    scan as scan_license,
)
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.secrets_scan import (
    scan as scan_secrets,
)
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.assets_index import (
    scan as scan_assets,
)

# NEW: static checker scanner (library-only; no CLI)
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.static_check import (
    scan as scan_static,
)

# Discovery used by scanners that expect a file list
from v2.backend.core.utils.code_bundles.code_bundles.execute.repo import (
    discover_repo_paths,
)

__all__ = [
    "_tool_versions",
    "_map_record_paths_inplace",
    "_append_records",
    "augment_manifest",
]


# ---------- small helpers ----------

def _tool_versions() -> dict:
    return {
        "python": sys.version.split()[0],
        "time_utc": datetime.now(timezone.utc).isoformat(),
    }


def _map_record_paths_inplace(items: List[dict], project_root: Path) -> None:
    """Normalize common path-like fields in-place to repo-relative forward-slash paths."""
    root = Path(project_root).resolve()
    pathish_keys = ("path", "file", "source", "target", "rel", "repo_path")
    for it in items or []:
        if not isinstance(it, dict):
            continue
        for k in pathish_keys:
            v = it.get(k)
            if isinstance(v, str):
                try:
                    it[k] = str(Path(v).resolve().relative_to(root).as_posix())
                except Exception:
                    # leave as-is if not under root
                    pass


def _append_records(manifest: dict, family: str, items: List[dict]) -> None:
    manifest.setdefault("analysis", {})
    manifest["analysis"].setdefault(family, [])
    manifest["analysis"][family].extend(items or [])


def _safe_list(result: Any) -> List[dict]:
    if result is None:
        return []
    if isinstance(result, list):
        return [x for x in result if isinstance(x, dict)]
    if isinstance(result, dict):
        return [result]
    return []


def _family_enabled(cfg: NS, family: str) -> bool:
    """Respect metadata_emission.* policy."""
    conf = getattr(cfg, "config", {}) or {}
    me = (conf.get("metadata_emission", {}) or {})
    policy = me.get(family)
    return policy in ("manifest", "both")


# ---------- scanner adapter ----------

def _adapt_and_call(
    scanner_fn,
    project_root: Path,
    cfg: NS,
    discovered_paths: List[Path],
    discovered_pairs: List[Tuple[Path, str]],
) -> Any:
    """
    Call scanner functions with varying signatures safely.

    Supported parameter names:
      - root | repo_root | project_root | src_root -> project_root
      - cfg -> cfg (namespace)
      - config | conf -> cfg.config (dict)
      - discovered -> discovered_pairs  (List[Tuple[Path, str]])
      - paths | files -> discovered_paths (List[Path])

    Falls back to positional in the same order if needed.
    """
    sig = inspect.signature(scanner_fn)
    params = list(sig.parameters.values())

    kwargs = {}
    for p in params:
        n = p.name
        if n in ("root", "repo_root", "project_root", "src_root"):
            kwargs[n] = project_root
        elif n == "cfg":
            kwargs[n] = cfg
        elif n in ("config", "conf"):
            kwargs[n] = getattr(cfg, "config", {})
        elif n == "discovered":
            kwargs[n] = discovered_pairs
        elif n in ("paths", "files"):
            kwargs[n] = discovered_paths

    # If all required are covered, call with kwargs
    if all(
        (p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY) and (p.default is not p.empty))
        or (p.name in kwargs)
        or (p.kind in (p.VAR_KEYWORD, p.VAR_POSITIONAL))
        for p in params
    ):
        return scanner_fn(**kwargs)

    # Fallback to positional mapping
    pos_args: List[Any] = []
    for p in params:
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        if p.name in ("root", "repo_root", "project_root", "src_root"):
            pos_args.append(project_root)
        elif p.name == "cfg":
            pos_args.append(cfg)
        elif p.name in ("config", "conf"):
            pos_args.append(getattr(cfg, "config", {}))
        elif p.name == "discovered":
            pos_args.append(discovered_pairs)
        elif p.name in ("paths", "files"):
            pos_args.append(discovered_paths)
        elif p.default is not p.empty:
            # let default apply by omitting a positional
            pass
        else:
            raise TypeError(
                f"Cannot satisfy parameter '{p.name}' for scanner {scanner_fn.__module__}.{scanner_fn.__name__}"
            )
    return scanner_fn(*pos_args)


# ---------- main augmentation ----------

def augment_manifest(cfg: NS, manifest: dict) -> dict:
    """
    Run the scanner set and append their outputs under manifest['analysis'][family].
    Families are aligned with your config's metadata_emission keys.
    """
    project_root = Path(getattr(cfg, "project_root", ".")).resolve()
    conf = getattr(cfg, "config", {}) or {}
    include_globs: List[str] = conf.get("include_globs", []) or []
    exclude_globs: List[str] = conf.get("exclude_globs", []) or []
    segment_excludes: List[str] = conf.get("segment_excludes", []) or []

    # Pre-discover once
    discovered_paths: List[Path] = list(
        discover_repo_paths(project_root, include_globs, exclude_globs, segment_excludes)
    )
    # And build the RepoItem tuples some scanners expect: (local_path, repo_relative_posix)
    discovered_pairs: List[Tuple[Path, str]] = []
    for p in discovered_paths:
        try:
            rel = p.resolve().relative_to(project_root).as_posix()
        except Exception:
            # If somehow outside root, fallback to name; scanner mostly cares about .gitignore hits
            rel = p.name
        discovered_pairs.append((p, rel))

    manifest = dict(manifest or {})
    manifest.setdefault("meta", {})
    manifest["meta"]["tools"] = _tool_versions()

    scans: List[tuple[str, List[dict]]] = []

    # Git repository info
    if _family_enabled(cfg, "git"):
        items = _safe_list(_adapt_and_call(scan_git, project_root, cfg, discovered_paths, discovered_pairs))
        _map_record_paths_inplace(items, project_root)
        scans.append(("git", items))

    # Dependencies
    if _family_enabled(cfg, "deps"):
        items = _safe_list(_adapt_and_call(scan_dependencies, project_root, cfg, discovered_paths, discovered_pairs))
        _map_record_paths_inplace(items, project_root)
        scans.append(("deps", items))

    # Entrypoints / execution surface
    if _family_enabled(cfg, "entrypoints"):
        items = _safe_list(_adapt_and_call(scan_entrypoints, project_root, cfg, discovered_paths, discovered_pairs))
        _map_record_paths_inplace(items, project_root)
        scans.append(("entrypoints", items))

    # Environment files
    if _family_enabled(cfg, "env"):
        items = _safe_list(_adapt_and_call(scan_env, project_root, cfg, discovered_paths, discovered_pairs))
        _map_record_paths_inplace(items, project_root)
        scans.append(("env", items))

    # SQL surface
    if _family_enabled(cfg, "sql"):
        items = _safe_list(_adapt_and_call(scan_sql, project_root, cfg, discovered_paths, discovered_pairs))
        _map_record_paths_inplace(items, project_root)
        scans.append(("sql", items))

    # HTML surface
    if _family_enabled(cfg, "html"):
        items = _safe_list(_adapt_and_call(scan_html, project_root, cfg, discovered_paths, discovered_pairs))
        _map_record_paths_inplace(items, project_root)
        scans.append(("html", items))

    # JS/TS surface
    if _family_enabled(cfg, "js"):
        items = _safe_list(_adapt_and_call(scan_js_ts, project_root, cfg, discovered_paths, discovered_pairs))
        _map_record_paths_inplace(items, project_root)
        scans.append(("js", items))

    # Code owners -> family key 'codeowners'
    if _family_enabled(cfg, "codeowners"):
        items = _safe_list(_adapt_and_call(scan_owners, project_root, cfg, discovered_paths, discovered_pairs))
        _map_record_paths_inplace(items, project_root)
        scans.append(("codeowners", items))

    # Docs coverage
    if _family_enabled(cfg, "docs"):
        items = _safe_list(_adapt_and_call(scan_doc_coverage, project_root, cfg, discovered_paths, discovered_pairs))
        _map_record_paths_inplace(items, project_root)
        scans.append(("docs", items))

    # Complexity / quality
    if _family_enabled(cfg, "quality"):
        items = _safe_list(_adapt_and_call(scan_complexity, project_root, cfg, discovered_paths, discovered_pairs))
        _map_record_paths_inplace(items, project_root)
        scans.append(("quality", items))

    # Secrets
    if _family_enabled(cfg, "secrets"):
        items = _safe_list(_adapt_and_call(scan_secrets, project_root, cfg, discovered_paths, discovered_pairs))
        _map_record_paths_inplace(items, project_root)
        scans.append(("secrets", items))

    # Licenses
    if _family_enabled(cfg, "license"):
        items = _safe_list(_adapt_and_call(scan_license, project_root, cfg, discovered_paths, discovered_pairs))
        _map_record_paths_inplace(items, project_root)
        scans.append(("license", items))

    # Assets (images/binaries)
    if _family_enabled(cfg, "asset"):
        items = _safe_list(_adapt_and_call(scan_assets, project_root, cfg, discovered_paths, discovered_pairs))
        _map_record_paths_inplace(items, project_root)
        scans.append(("asset", items))

    # Static checker (new) — gated by metadata_emission.static_check
    if _family_enabled(cfg, "static_check"):
        # static_check takes (project_root, exclude=...) directly; no need for discovered pairs
        exclude_globs: List[str] = conf.get("exclude_globs", []) or []
        items = _safe_list(scan_static(project_root, exclude=exclude_globs))
        _map_record_paths_inplace(items, project_root)
        scans.append(("static_check", items))

    # Append into manifest
    for fam, items in scans:
        _append_records(manifest, fam, items)

    return manifest





