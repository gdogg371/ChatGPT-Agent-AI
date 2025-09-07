from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Tuple

# PyYAML is required to read config/packager.yml.
try:
    import yaml  # type: ignore
except Exception as e:  # pragma: no cover
    raise ImportError("PyYAML is required to read config/packager.yml for the registry.") from e


# ──────────────────────────────────────────────────────────────────────────────
# Minimal error type (keeps failure mode explicit; no extra features)
# ──────────────────────────────────────────────────────────────────────────────
class RegistryConfigError(RuntimeError):
    """Configuration problem in config/packager.yml (registry.*)."""


# ──────────────────────────────────────────────────────────────────────────────
# Reducer implementations (referenced by reducer tokens in YAML)
# ──────────────────────────────────────────────────────────────────────────────

def _generic_counter(items: List[dict], family: str) -> dict:
    return {
        "family": family,
        "stats": {"count": len(items)},
        "sample": items[:5],
    }


def _ast_symbols_reducer(items: List[dict]) -> dict:
    kinds = Counter([x.get("payload", {}).get("kind", "unknown") for x in items])
    return {"family": "ast_symbols", "stats": {"count": len(items), "kinds": dict(kinds)}}


def _ast_imports_reducer(items: List[dict]) -> dict:
    modules = Counter([x.get("payload", {}).get("module", "unknown") for x in items])
    return {"family": "ast_imports", "stats": {"count": len(items), "top_modules": modules.most_common(10)}}


def _ast_calls_reducer(items: List[dict]) -> dict:
    names = Counter([x.get("payload", {}).get("name", "unknown") for x in items])
    return {"family": "ast_calls", "stats": {"count": len(items), "top_calls": names.most_common(15)}}


def _deps_reducer(items: List[dict]) -> dict:
    pkgs = Counter([x.get("payload", {}).get("name", "unknown") for x in items])
    return {"family": "deps", "stats": {"count": len(items), "packages": pkgs.most_common(50)}}


def _entrypoints_reducer(items: List[dict]) -> dict:
    kinds = Counter([x.get("payload", {}).get("kind", "unknown") for x in items])
    return {"family": "entrypoints", "stats": {"count": len(items), "kinds": dict(kinds)}}


def _docs_reducer(items: List[dict]) -> dict:
    vals = [
        x.get("payload", {}).get("coverage", 0.0)
        for x in items
        if isinstance(x.get("payload", {}).get("coverage", None), (int, float))
    ]
    avg = sum(vals) / max(1, len(vals)) if vals else 0.0
    return {"family": "docs", "stats": {"count": len(items), "avg_coverage": round(avg, 3)}}


def _quality_reducer(items: List[dict]) -> dict:
    vals = [
        x.get("payload", {}).get("complexity", 0)
        for x in items
        if isinstance(x.get("payload", {}).get("complexity", None), (int, float))
    ]
    avg = sum(vals) / max(1, len(vals)) if vals else 0.0
    return {"family": "quality", "stats": {"count": len(items), "avg_complexity": round(avg, 3)}}


def _sql_reducer(items: List[dict]) -> dict:
    kinds = Counter([x.get("payload", {}).get("kind", "unknown") for x in items])
    return {"family": "sql", "stats": {"count": len(items), "kinds": dict(kinds)}}


def _static_reducer(items: List[dict]) -> dict:
    sev = Counter([x.get("severity", "unknown") for x in items])
    checks = Counter([x.get("check", "unknown") for x in items])
    codes = Counter([x.get("code", "unknown") for x in items])
    return {
        "family": "static",
        "stats": {
            "count": len(items),
            "by_severity": dict(sev),
            "top_checks": checks.most_common(15),
            "top_codes": codes.most_common(15),
        },
    }


# Token → reducer function registry (the only in-code mapping)
_TOKEN_TO_REDUCER: Dict[str, Callable[[List[dict]], dict]] = {
    "ast_symbols": _ast_symbols_reducer,
    "ast_imports": _ast_imports_reducer,
    "ast_calls": _ast_calls_reducer,
    "deps": _deps_reducer,
    "entrypoints": _entrypoints_reducer,
    "docs": _docs_reducer,
    "quality": _quality_reducer,
    "sql": _sql_reducer,
    "static": _static_reducer,
    # "generic_counter" handled specially (captures family name)
}


# ──────────────────────────────────────────────────────────────────────────────
# YAML loading (fixed location: config/packager.yml; no walking)
# ──────────────────────────────────────────────────────────────────────────────

_CFG_PATH = Path("config") / "packager.yml"  # resolve relative to current working directory


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise RegistryConfigError(f"Missing required file: {path.resolve()}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        raise RegistryConfigError(f"Failed to parse YAML at {path.resolve()}: {e}") from e
    if not isinstance(data, dict):
        raise RegistryConfigError(f"{path.resolve()} must parse to a mapping at top level.")
    return data


def _norm(s: str) -> str:
    return s.replace("-", "_").strip().lower()


def _build_reducers_from_tokens(tokens: Mapping[str, str]) -> Dict[str, Callable[[List[dict]], dict]]:
    """
    Convert reducer name tokens to callables.
    Unknown tokens and missing families fall back to generic_counter.
    """
    reducers: Dict[str, Callable[[List[dict]], dict]] = {}
    for family_raw, token_raw in tokens.items():
        family = _norm(family_raw)
        token = _norm(token_raw)
        if token == "generic_counter":
            reducers[family] = (lambda fam=family: (lambda items: _generic_counter(items, fam)))()
        else:
            fn = _TOKEN_TO_REDUCER.get(token)
            reducers[family] = fn if fn is not None else (lambda f=family: (lambda items: _generic_counter(items, f)))()
    return reducers


def _load_registry_from_yaml() -> Tuple[Dict[str, str], List[str], Dict[str, Callable[[List[dict]], dict]]]:
    """
    Return (aliases, canon_families, reducers) from config/packager.yml → registry.*.
    This function enforces only the keys needed to wire YAML into code; no extra controls.
    """
    root = _load_yaml(_CFG_PATH)

    reg = root.get("registry")
    if not isinstance(reg, dict):
        raise RegistryConfigError("Missing required key 'registry' in config/packager.yml.")

    # aliases
    aliases_raw = reg.get("aliases")
    if not isinstance(aliases_raw, dict):
        raise RegistryConfigError("Missing 'registry.aliases' mapping in config/packager.yml.")
    aliases: Dict[str, str] = {
        _norm(k): _norm(v)
        for k, v in aliases_raw.items()
        if isinstance(k, str) and isinstance(v, str)
    }

    # canonical families
    canon_raw = reg.get("canon_families")
    if not isinstance(canon_raw, list) or not all(isinstance(x, str) for x in canon_raw):
        raise RegistryConfigError("Missing 'registry.canon_families' list in config/packager.yml.")
    canon_families = [_norm(x) for x in canon_raw]

    # reducers
    reducers_raw = reg.get("reducers")
    if not isinstance(reducers_raw, dict):
        raise RegistryConfigError("Missing 'registry.reducers' mapping in config/packager.yml.")
    reducers = _build_reducers_from_tokens(reducers_raw)

    # ensure every canonical family has a reducer; default to generic_counter
    for fam in canon_families:
        reducers.setdefault(fam, (lambda f=fam: (lambda items: _generic_counter(items, f)))())

    return aliases, canon_families, reducers


# ──────────────────────────────────────────────────────────────────────────────
# Effective registry (populated exclusively from YAML)
# ──────────────────────────────────────────────────────────────────────────────

_ALIASES, _CANON_FAMILIES_LIST, _REDUCERS = _load_registry_from_yaml()
_CANON_FAMILIES = set(_CANON_FAMILIES_LIST)


# ──────────────────────────────────────────────────────────────────────────────
# Public API (unchanged)
# ──────────────────────────────────────────────────────────────────────────────

def canonicalize_family(name: str) -> str:
    if name in _ALIASES:
        return _ALIASES[name]
    dotted = name.replace(".", "_")
    if dotted in _ALIASES:
        return _ALIASES[dotted]
    return dotted


def get_reducer(family: str):
    fam = canonicalize_family(family)
    return _REDUCERS.get(fam, (lambda x: _generic_counter(x, fam)))


def zero_summary_for(family: str) -> dict:
    fam = canonicalize_family(family)
    return {"family": fam, "stats": {"count": 0}, "items": []}


