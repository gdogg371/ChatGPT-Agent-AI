from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Set, Tuple

import re

try:
    import yaml  # type: ignore
except Exception as e:  # pragma: no cover
    raise RuntimeError("pyyaml is required to load config/packager.yml") from e


@dataclass
class Config:
    repo_root: Path
    emitted_prefix: str
    emitted_dir: Path
    analysis_out_dir: Path

    publish_analysis: bool
    emit_ast: bool

    # Whole sections
    publish: Dict[str, Any]
    transport: Dict[str, Any]
    metadata_emission: Dict[str, str]
    analysis_filenames: Dict[str, str]
    family_aliases: Dict[str, str]
    controls: Dict[str, Any]
    limits: Dict[str, Any]

    # Derived / resolved
    design_manifest_dir: Path
    parts_index_path: Path
    emitter_policy: Dict[str, Any]  # {"mode": "all"} or {"mode": "set", "families": [..]}
    canonical_families: Set[str]


def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _req_str(d: Mapping[str, Any], key: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v:
        raise ValueError(f"Missing/invalid string for '{key}'")
    return v


def _get_bool(d: Mapping[str, Any], key: str, default: bool = False) -> bool:
    v = d.get(key, default)
    return bool(v)


def _family_canon_map(aliases: Mapping[str, str], metadata_emission: Mapping[str, str]) -> Dict[str, str]:
    """
    Build a canonicalization map that:
      - includes provided aliases
      - maps dotted AST names to canonical (e.g., 'ast.call' -> 'ast_calls')
      - maps obvious short forms if missing
    """
    canon = dict(aliases)

    # Dotted AST to snake form
    dotted_map = {
        "ast.call": "ast_calls",
        "ast.symbol": "ast_symbols",
        "ast.symbols": "ast_symbols",
        "ast.import": "ast_imports",
        "ast.import_from": "ast_imports",
        "edge.import": "ast_imports",
        "entrypoint": "entrypoints",
        "file": "ast_symbols",
        "class": "ast_symbols",
        "function": "ast_symbols",
        "call": "ast_calls",
        "import": "ast_imports",
        "import_from": "ast_imports",
        "from": "ast_imports",
        "artifact": "io_core",
        "manifest": "io_core",
    }
    for k, v in dotted_map.items():
        canon.setdefault(k, v)

    # Ensure every canonical family maps to itself
    for fam in metadata_emission.keys():
        canon.setdefault(fam, fam)

    return canon


def _canon_family(name: str, canon_map: Mapping[str, str]) -> str:
    if name in canon_map:
        return canon_map[name]
    dotted = name.replace(".", "_")
    return canon_map.get(dotted, dotted)


def _resolve_emitter_policy(cfg: Dict[str, Any],
                            canon_map: Mapping[str, str],
                            all_emit_families: Iterable[str]) -> Dict[str, Any]:
    """
    Decide which families to emit:
      - If publish.analysis.enabled and emitters == 'all' → all (where metadata_emission != 'none')
      - If publish.analysis.enabled and emitters.set present → that set (canonicalized + filtered)
      - Else if legacy publish_analysis: true → all
      - Else → none (empty set)
    """
    legacy = bool(cfg.get("publish_analysis"))
    publish = cfg.get("publish", {}) or {}
    analysis = publish.get("analysis", {}) or {}
    enabled = bool(analysis.get("enabled", False))
    emitters = analysis.get("emitters")

    # Helper: expand to canonical and filter to known families
    def as_set(obj) -> List[str]:
        if isinstance(obj, dict) and "set" in obj:
            seq = obj["set"]
        else:
            seq = obj
        if not isinstance(seq, (list, tuple, set)):
            return []
        acc: List[str] = []
        for x in seq:
            if not isinstance(x, str):
                continue
            cx = _canon_family(x, canon_map)
            acc.append(cx)
        # filter to families that are actually emitted (metadata_emission != none)
        return [x for x in acc if x in set(all_emit_families)]

    if enabled:
        if emitters == "all":
            return {"mode": "all"}
        elif isinstance(emitters, (list, dict)):
            fams = as_set(emitters)
            return {"mode": "set", "families": sorted(set(fams))}
        else:
            # enabled but unspecified -> treat as ALL
            return {"mode": "all"}

    if legacy:
        return {"mode": "all"}

    return {"mode": "set", "families": []}


def load_packager_config(repo_root: Path) -> Config:
    """
    Load config/packager.yml, resolve canonical families and emitter policy,
    compute analysis output paths, and return a structured Config.
    """
    cfg_path = repo_root / "config" / "packager.yml"
    raw = _load_yaml(cfg_path)

    emitted_prefix = _req_str(raw, "emitted_prefix")
    emit_ast = _get_bool(raw, "emit_ast", True)
    include_globs = raw.get("include_globs", []) or []
    exclude_globs = raw.get("exclude_globs", []) or []
    segment_excludes = raw.get("segment_excludes", []) or []

    publish = raw.get("publish", {}) or {}
    transport = raw.get("transport", {}) or {}
    metadata_emission = raw.get("metadata_emission", {}) or {}
    analysis_filenames = raw.get("analysis_filenames", {}) or {}
    family_aliases = raw.get("family_aliases", {}) or {}
    controls = raw.get("controls", {}) or {}
    limits = raw.get("limits", {}) or {}

    # Canonicalize families and compute the set that are eligible to be emitted
    canon_map = _family_canon_map(family_aliases, metadata_emission)
    eligible_families = {fam for fam, mode in metadata_emission.items() if str(mode).lower() != "none"}

    emitter_policy = _resolve_emitter_policy(raw, canon_map, eligible_families)

    emitted_dir = (repo_root / emitted_prefix).resolve()
    analysis_out_dir = emitted_dir / "analysis"

    design_manifest_dir = repo_root / "output" / "design_manifest"
    parts_index_path = design_manifest_dir / "design_manifest_parts_index.json"

    return Config(
        repo_root=repo_root,
        emitted_prefix=emitted_prefix,
        emitted_dir=emitted_dir,
        analysis_out_dir=analysis_out_dir,
        publish_analysis=bool(raw.get("publish_analysis", False)),
        emit_ast=emit_ast,
        publish=publish,
        transport=transport,
        metadata_emission=metadata_emission,
        analysis_filenames=analysis_filenames,
        family_aliases=canon_map,
        controls=controls,
        limits=limits,
        design_manifest_dir=design_manifest_dir,
        parts_index_path=parts_index_path,
        emitter_policy=emitter_policy,
        canonical_families=eligible_families,
    )
