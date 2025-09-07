# File: v2/backend/core/utils/code_bundles/code_bundles/src/packager/core/record_adapter.py
# Purpose: stdlib-only normalizer that wraps arbitrary scanner/plugin dicts into
#          a canonical "scanner.record.v1" envelope, driven strictly by config/packager.yml.
#
# Key behavior:
# - Prefer record_type → kind mapping (from scanner_output.record_type_kind_map) over any raw "kind".
# - Do NOT consume reader.aliases (analysis-facing). Use ONLY scanner_output.kind_aliases (optional, minimal).
# - For ast.xref rows: keep top-level kind from mapping (e.g., "edge.import") and place raw subtype
#   ("import" | "import_from") into edge.type.
# - For ast.symbol rows: keep kind == "ast.symbol" and place raw subtype ("class" | "function" | ...)
#   into symbol.kind.
#
# Notes:
# - No hardcoded paths or mappings. The YAML is loaded from repo_root/config/packager.yml,
#   with repo_root discovered via v2.backend.core.configuration.loader.get_repo_root().
# - If scanner_output.record_type_kind_map is missing or unmapped for a seen record_type,
#   the record is wrapped with status.parse_error = true (reason set), not guessed.
# - Meta/run records listed in scanner_output.exclude_record_types are ignored (return None).
# - Lines are 1-based; columns are 0-based. Paths are repo-relative POSIX (forward slashes).
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Set

import yaml  # PyYAML is already used in the project

from v2.backend.core.utils.code_bundles.code_bundles.execute.loader import get_repo_root

__all__ = [
    "Producer",
    "WrapperPolicy",
    "load_wrapper_policy",
    "wrap_record",
    "validate_v1",
]

_REQUIRED_V1_KEYS = ("schema", "kind", "scope", "data", "scanner", "detected_at", "fp", "status")


@dataclass(frozen=True)
class Producer:
    """Identity of the producing component (scanner or plugin)."""
    name: str
    version: str = "unknown"


@dataclass(frozen=True)
class WrapperPolicy:
    """Policy loaded from YAML for the record wrapper."""
    require_v1: bool
    record_type_kind_map: Mapping[str, str]
    scope_overrides: Mapping[str, str]
    exclude_record_types: Set[str]
    kind_aliases: Mapping[str, str]  # from scanner_output.kind_aliases ONLY


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_kind(kind: str, aliases: Optional[Mapping[str, str]] = None) -> str:
    """
    Normalize family of 'family.subkind' using aliases. If kind has no dot,
    it will be returned as-is after family normalization.
    """
    if not isinstance(kind, str) or not kind:
        return "unknown.item"
    fam, dot, sub = kind.partition(".")
    fam2 = aliases.get(fam, fam) if aliases else fam
    return f"{fam2}.{sub}" if dot else fam2


def _guess_path(raw: Mapping[str, Any]) -> Optional[str]:
    for k in ("path", "src_path", "file", "file_path", "module_path"):
        v = raw.get(k)
        if isinstance(v, str) and v:
            return v.replace("\\", "/")
    return None


def _extract_loc(raw: Mapping[str, Any]) -> Optional[Dict[str, int]]:
    def _gi(*keys: str) -> Optional[int]:
        for k in keys:
            v = raw.get(k)
            if isinstance(v, int):
                return v
            if isinstance(v, str) and v.isdigit():
                return int(v)
        return None

    line = _gi("line", "lineno")
    end_line = _gi("end_line", "end_lineno")
    col = _gi("col", "col_offset", "column")
    end_col = _gi("end_col", "end_col_offset")
    loc: Dict[str, int] = {}
    if line is not None:
        loc["line"] = line
    if col is not None:
        loc["col"] = col
    if end_line is not None:
        loc["end_line"] = end_line
    if end_col is not None:
        loc["end_col"] = end_col
    return loc or None


def _infer_scope(kind: str) -> str:
    if kind.endswith(".summary") or kind in ("git.repo",):
        return "repo"
    fam, _, sub = kind.partition(".")
    if fam == "edge" or sub in ("xref", "edge"):
        return "edge"
    if fam == "ast" and (sub.startswith("symbol") or sub == "docstring"):
        return "symbol"
    return "file"


def _compute_fp(
    kind: str,
    path: Optional[str],
    code: Optional[str],
    message: Optional[str],
    loc: Optional[Mapping[str, int]],
) -> str:
    line = str(loc.get("line")) if loc and "line" in loc else ""
    core = "|".join([kind or "", path or "", code or "", message or "", line])
    return "sha256:" + sha256(core.encode("utf-8")).hexdigest()



def validate_v1(env: Mapping[str, Any]) -> None:
    """
    Raise ValueError if the envelope does not satisfy the minimal v1 contract.
    Required: schema, kind, scope, data (dict), scanner{name,version}, detected_at, fp, status.
    """
    for k in _REQUIRED_V1_KEYS:
        if k not in env:
            raise ValueError(f"missing required key: {k}")
    if env["schema"] != "scanner.record.v1":
        raise ValueError("schema must be 'scanner.record.v1'")
    if not isinstance(env.get("data"), dict):
        raise ValueError("data must be an object")
    scanner = env.get("scanner")
    if not (isinstance(scanner, dict) and isinstance(scanner.get("name"), str) and isinstance(scanner.get("version"), str)):
        raise ValueError("scanner must be an object with name and version")
    status = env.get("status")
    if not (isinstance(status, dict) and {"ok", "parse_error", "suppressed", "reason"} <= set(status.keys())):
        raise ValueError("status must contain ok, parse_error, suppressed, reason")


# -------- YAML policy loading (strict, using the same discovery as the rest of the codebase) --------

def _load_yaml(path: Path) -> Dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def load_wrapper_policy() -> WrapperPolicy:
    """
    Load WrapperPolicy from repo_root/config/packager.yml, discovered via get_repo_root().
    This function is strict: required sections/keys must exist; otherwise it raises.
    """
    repo_root = get_repo_root()
    cfg_path = (repo_root / "config" / "packager.yml").resolve()
    if not cfg_path.exists():
        raise FileNotFoundError(f"missing config/packager.yml at {cfg_path}")

    cfg = _load_yaml(cfg_path)

    # Append-only section defined for the wrapper.
    so = cfg.get("scanner_output")
    if not isinstance(so, dict):
        raise ValueError("missing required 'scanner_output' section in config/packager.yml")

    require_v1 = bool(so.get("require_v1", True))

    rtkm = so.get("record_type_kind_map")
    if not isinstance(rtkm, dict) or not rtkm:
        raise ValueError("scanner_output.record_type_kind_map must be a non-empty mapping")

    scope_overrides = so.get("scope_overrides") or {}
    if not isinstance(scope_overrides, dict):
        raise ValueError("scanner_output.scope_overrides must be a mapping")

    excl = so.get("exclude_record_types") or []
    if not isinstance(excl, (list, tuple)):
        raise ValueError("scanner_output.exclude_record_types must be a list")
    exclude_record_types: Set[str] = {str(x) for x in excl}

    # Use ONLY scanner_output.kind_aliases for envelope kind normalization (NOT reader.aliases).
    kind_aliases = so.get("kind_aliases") or {}
    if not isinstance(kind_aliases, dict):
        kind_aliases = {}

    return WrapperPolicy(
        require_v1=require_v1,
        record_type_kind_map={str(k): str(v) for k, v in rtkm.items()},
        scope_overrides={str(k): str(v) for k, v in scope_overrides.items()},
        exclude_record_types=exclude_record_types,
        kind_aliases={str(k): str(v) for k, v in kind_aliases.items()},
    )


# -------- Public API --------

def wrap_record(
    raw: Mapping[str, Any],
    producer: Producer,
    *,
    detected_at: Optional[str] = None,
    policy: Optional[WrapperPolicy] = None,
) -> Optional[Dict[str, Any]]:
    """
    Wrap a raw producer dict into a 'scanner.record.v1' envelope, using YAML-driven policy.

    Returns:
      - dict: normalized envelope ready to persist, or
      - None: when the record should be ignored (record_type excluded by policy).

    Behavior:
      - If raw.schema == 'scanner.record.v1': validate + enrich (detected_at/fp) and return.
      - Else resolve 'kind' via (in order):
          1) record_type mapping from policy.record_type_kind_map (if present),
          2) raw['kind'] (then normalized with policy.kind_aliases),
          3) else mark parse_error with reason.
      - Scope is taken from policy.scope_overrides or inferred by convention.
      - Known fields (path, loc, severity, code, message, symbol/edge) are lifted; remainder kept under data.
      - Stable fingerprint 'fp' is computed over key fields.
    """
    pol = policy or load_wrapper_policy()

    # Skip meta/run records explicitly excluded by YAML
    rt = raw.get("record_type")
    if isinstance(rt, str) and rt in pol.exclude_record_types:
        return None

    # Pass-through if already v1 (validate + enrich only)
    if raw.get("schema") == "scanner.record.v1":
        env = dict(raw)  # shallow copy
        if "detected_at" not in env:
            env["detected_at"] = detected_at or _utc_now_iso()
        if "fp" not in env:
            kind = env.get("kind", "")
            path = env.get("path")
            code = env.get("code")
            message = env.get("message")
            loc = env.get("loc")
            env["fp"] = _compute_fp(kind, path, code, message, loc if isinstance(loc, dict) else None)
        validate_v1(env)
        return env

    status = {"ok": True, "parse_error": False, "suppressed": False, "reason": None}

    # 1) Determine canonical kind (prefer record_type mapping)
    kind: Optional[str] = None
    if isinstance(rt, str) and rt in pol.record_type_kind_map:
        kind = pol.record_type_kind_map[rt]
    else:
        rk = raw.get("kind")
        if isinstance(rk, str) and rk:
            kind = rk
        else:
            kind = f"unknown.{rt}" if isinstance(rt, str) else "unknown.item"
            status = {
                "ok": False,
                "parse_error": True,
                "suppressed": False,
                "reason": "record_type_unmapped" if isinstance(rt, str) else "no_kind_or_record_type",
            }
    kind = _normalize_kind(kind, pol.kind_aliases)

    # 2) Scope (override or infer)
    scope = pol.scope_overrides.get(kind) if pol.scope_overrides else None
    if not scope:
        scope = _infer_scope(kind)

    # 3) Common lifts
    path = _guess_path(raw)
    loc = _extract_loc(raw)

    severity = raw.get("severity") if isinstance(raw.get("severity"), str) else None
    code = None
    for k in ("code", "rule", "check", "id"):
        v = raw.get(k)
        if isinstance(v, str):
            code = v
            break
    message = None
    for k in ("message", "note", "reason", "desc", "description"):
        v = raw.get(k)
        if isinstance(v, str):
            message = v
            break

    # 4) Optional symbol/edge shaping
    symbol = None
    # For ast.symbol (by record_type) OR any top-level ast.symbol.* kind, capture symbol metadata
    if (rt == "ast.symbol") or kind.startswith("ast.symbol"):
        if raw.get("name") or raw.get("symbol") or raw.get("qualname"):
            # subtype from the raw payload (e.g., "class", "function", "variable", etc.)
            subtype = raw.get("sym_kind") or raw.get("symbol_kind")
            if subtype is None and isinstance(raw.get("kind"), str) and raw.get("kind") != kind:
                subtype = raw.get("kind")
            symbol = {
                "name": raw.get("name") or raw.get("symbol"),
                "qualname": raw.get("qualname"),
                "kind": subtype,
            }
            symbol = {k: v for k, v in symbol.items() if v}

    edge = None
    # For ast.xref, map subtype ("import"|"import_from") into edge.type
    if (rt == "ast.xref") or kind.startswith("edge."):
        edge_type = None
        raw_kind = raw.get("kind") if isinstance(raw.get("kind"), str) else None
        if rt == "ast.xref":
            if raw_kind in ("import", "import_from"):
                edge_type = raw_kind
            else:
                edge_type = "import"  # default for xref imports when unspecified
        # Build edge object
        to_name = raw.get("module") or raw.get("to") or raw.get("dst_module")
        edge = {
            **({"type": edge_type} if edge_type else {}),
            "from": {"path": path} if path else {},
            "to": {"name": to_name, "external": bool(raw.get("external"))},
        }
        edge = {k: v for k, v in edge.items() if v}

    # 5) Envelope
    detected = detected_at or _utc_now_iso()
    envelope: Dict[str, Any] = {
        "schema": "scanner.record.v1",
        "kind": kind,
        "scope": scope,
        **({"path": path} if path else {}),
        **({"loc": loc} if loc else {}),
        **({"severity": severity} if severity else {}),
        **({"code": code} if code else {}),
        **({"message": message} if message else {}),
        **({"symbol": symbol} if symbol else {}),
        **({"edge": edge} if edge else {}),
        "data": {},
        "scanner": {"name": producer.name, "version": producer.version},
        "detected_at": detected,
        "status": status,
    }

    # 6) Preserve remainder under data/*
    lifted = {
        "schema",
        "kind",
        "record_type",
        "path",
        "src_path",
        "file",
        "file_path",
        "module_path",
        "line",
        "lineno",
        "end_line",
        "end_lineno",
        "col",
        "col_offset",
        "end_col",
        "end_col_offset",
        "severity",
        "code",
        "rule",
        "check",
        "id",
        "message",
        "note",
        "reason",
        "desc",
        "description",
        "name",
        "symbol",
        "qualname",
        "sym_kind",
        "symbol_kind",
        "module",
        "to",
        "dst_module",
        "external",
    }
    data = {k: v for k, v in raw.items() if k not in lifted}
    if data:
        envelope["data"] = data

    # 7) Fingerprint + validate
    envelope["fp"] = _compute_fp(kind, path, code, message, loc)
    validate_v1(envelope)
    return envelope

