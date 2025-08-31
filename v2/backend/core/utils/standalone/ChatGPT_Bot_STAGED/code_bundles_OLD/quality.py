# src/packager/quality.py
from __future__ import annotations

import json
import os
import platform
import sys
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

__all__ = ["QualityParser", "QualityReport"]

JSONLike = Union[dict, list, str, int, float, bool, None]
Blob = Union[Dict[str, Any], str]


# ---------- data models (lightweight; stdlib only) ----------

@dataclass
class LintFinding:
    path: Optional[str]
    line: Optional[int]
    col: Optional[int]
    code: Optional[str]
    message: str
    severity: str  # "error" | "warning" | "info" | "hint"
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TypeEntry:
    path: Optional[str]
    symbol: Optional[str]
    inferred_type: Optional[str]
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CoverageFile:
    path: str
    covered_lines: List[int] = field(default_factory=list)
    missing_lines: List[int] = field(default_factory=list)
    pct: Optional[float] = None


@dataclass
class CoverageSession:
    timestamp: Optional[str]
    totals: Dict[str, Any]  # {"lines": {"covered": int, "missed": int, "pct": float}} (minimal consistent shape)
    files: List[CoverageFile]
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TraceEvent:
    ts: Optional[float]     # timestamp (ms or s; we do not convert)
    name: Optional[str]
    cat: Optional[str]
    dur: Optional[float]    # duration
    args: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class QualityReport:
    lint: Dict[str, Any]
    types: Dict[str, Any]
    coverage: Dict[str, Any]
    traces: Dict[str, Any]
    config: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lint": self.lint,
            "types": self.types,
            "coverage": self.coverage,
            "traces": self.traces,
            "config": self.config,
        }


# ---------- parser implementation ----------

class QualityParser:
    """
    Defensive, class-based parser for quality signals.
    Accepts lists of dicts or strings (JSON/JSONL/plaintext), and normalizes to
    compact, schema-stable summaries for packing into the bundle.

    All methods are best-effort: bad or unknown shapes are tolerated and folded
    into 'raw' fields rather than throwing.
    """

    # ----------------- public API -----------------

    def parse_lint(self, blobs: List[Blob]) -> Dict[str, Any]:
        """
        Normalize linter findings from a mix of tools (pylint/flake8/ruff/gh-annotations).
        Returns:
            {
              "tool": "<best_guess|external>",
              "findings": [LintFinding... as dicts],
              "summary": {"counts": {"error": n, "warning": n, "info": n, "hint": n}, "total": n}
            }
        """
        items = list(self._iter_json_objects(blobs))
        tool = self._guess_lint_tool(items)
        findings: List[LintFinding] = []

        for obj in items:
            # Heuristics for common shapes
            # GitHub annotation-like: {"path","start_line","annotation_level","message","title","raw_details"}
            if {"path", "message"} & obj.keys():
                severity = self._map_severity(
                    obj.get("level")
                    or obj.get("annotation_level")
                    or obj.get("severity")
                    or obj.get("type")
                )
                code = obj.get("rule") or obj.get("code") or obj.get("title")
                findings.append(
                    LintFinding(
                        path=obj.get("path") or obj.get("file"),
                        line=_as_int(obj.get("line") or obj.get("start_line")),
                        col=_as_int(obj.get("col") or obj.get("start_column")),
                        code=_as_str(code),
                        message=_as_str(obj.get("message") or obj.get("text") or "") or "",
                        severity=severity,
                        raw=obj,
                    )
                )
                continue

            # Ruff/Flake8-like: {"code":"F401","filename":"...","message":"...","location":{"row":..,"column":..}}
            if "code" in obj and ("filename" in obj or "file" in obj or "path" in obj):
                loc = obj.get("location") or {}
                findings.append(
                    LintFinding(
                        path=obj.get("filename") or obj.get("file") or obj.get("path"),
                        line=_as_int(obj.get("line") or loc.get("row")),
                        col=_as_int(obj.get("column") or loc.get("column")),
                        code=_as_str(obj.get("code")),
                        message=_as_str(obj.get("message") or obj.get("description") or "") or "",
                        severity=self._map_severity(obj.get("severity") or obj.get("type")),
                        raw=obj,
                    )
                )
                continue

            # Pylint-like: {"type":"convention","module":"x","obj":"f","line":1,"column":0,"message-id":"C0114","message":"...","path":"..."}
            if "message-id" in obj or ("module" in obj and "message" in obj):
                findings.append(
                    LintFinding(
                        path=obj.get("path") or obj.get("filename"),
                        line=_as_int(obj.get("line")),
                        col=_as_int(obj.get("column")),
                        code=_as_str(obj.get("message-id") or obj.get("symbol") or obj.get("code")),
                        message=_as_str(obj.get("message") or "") or "",
                        severity=self._map_severity(obj.get("type")),
                        raw=obj,
                    )
                )
                continue

            # Fallback: keep what we can
            findings.append(
                LintFinding(
                    path=_as_str(obj.get("path") or obj.get("file") or obj.get("filename")),
                    line=_as_int(obj.get("line")),
                    col=_as_int(obj.get("col")),
                    code=_as_str(obj.get("code")),
                    message=_as_str(obj.get("message") or obj.get("title") or obj.get("text") or "") or "",
                    severity=self._map_severity(obj.get("severity") or obj.get("type")),
                    raw=obj,
                )
            )

        counts = {"error": 0, "warning": 0, "info": 0, "hint": 0}
        for f in findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1

        return {
            "tool": tool,
            "findings": [asdict(f) for f in findings],
            "summary": {"counts": counts, "total": sum(counts.values())},
        }

    def parse_type_map(self, blobs: List[Blob]) -> Dict[str, Any]:
        """
        Normalize static type inference maps (mypy/pyright or custom).
        Returns: {"tool": "<guess|external>", "entries": [TypeEntry...]}
        """
        items = list(self._iter_json_objects(blobs))
        tool = self._guess_type_tool(items)
        entries: List[TypeEntry] = []

        for obj in items:
            # mypy JSON errors can be varied; for "type maps" we accept generic entries
            sym = obj.get("symbol") or obj.get("name") or obj.get("target")
            inferred = obj.get("inferred") or obj.get("type") or obj.get("annotation")
            entries.append(
                TypeEntry(
                    path=_as_str(obj.get("path") or obj.get("file") or obj.get("filename")),
                    symbol=_as_str(sym),
                    inferred_type=_as_str(inferred),
                    raw=obj,
                )
            )

        return {"tool": tool, "entries": [asdict(e) for e in entries]}

    def parse_coverage(self, blobs: List[Blob]) -> Dict[str, Any]:
        """
        Normalize coverage sessions. Supports coverage.py JSON-ish shapes (best-effort).
        Returns:
            {
              "sessions": [CoverageSession... as dicts],
              "aggregate": {"lines_pct": <float>|None}
            }
        """
        sessions: List[CoverageSession] = []
        for obj in self._iter_json_objects(blobs):
            files: List[CoverageFile] = []
            totals = {"lines": {"covered": None, "missed": None, "pct": None}}

            # Try coverage.py JSON: {"files": {"path": {"executed_lines":[...], "missing_lines":[...]}}, "totals": {...}}
            fobj = obj.get("files") or obj.get("filesummary") or {}
            if isinstance(fobj, dict):
                for path, meta in fobj.items():
                    covered = list(_as_int_list(meta.get("executed_lines") or meta.get("covered_lines") or []))
                    missing = list(_as_int_list(meta.get("missing_lines") or []))
                    pct = _as_float(meta.get("summary", {}).get("percent_covered")
                                    or meta.get("percent_covered")
                                    or meta.get("pct"))
                    files.append(CoverageFile(path=path, covered_lines=covered, missing_lines=missing, pct=pct))

            t = obj.get("totals") or {}
            lines_pct = _as_float(t.get("percent_covered") or t.get("lines_percent") or t.get("pct"))
            totals["lines"] = {
                "covered": _as_int(t.get("covered_lines") or t.get("covered")),
                "missed": _as_int(t.get("missing_lines") or t.get("missed")),
                "pct": lines_pct,
            }

            sessions.append(
                CoverageSession(
                    timestamp=_as_str(obj.get("created_at") or obj.get("timestamp")),
                    totals=totals,
                    files=files,
                    raw=obj,
                )
            )

        # Aggregate (weighted by covered+missed if available)
        agg_pct = _weighted_lines_pct(sessions)
        return {"sessions": [_coverage_session_to_dict(s) for s in sessions], "aggregate": {"lines_pct": agg_pct}}

    def parse_traces(self, blobs: List[Blob]) -> Dict[str, Any]:
        """
        Normalize execution traces (Chrome trace style or custom).
        Returns:
            {"events": [TraceEvent... as dicts], "summary": {"count": n}}
        """
        events: List[TraceEvent] = []
        for obj in self._iter_json_objects(blobs):
            # Chrome trace style often has "traceEvents": [...]
            if "traceEvents" in obj and isinstance(obj["traceEvents"], list):
                for ev in obj["traceEvents"]:
                    events.append(
                        TraceEvent(
                            ts=_as_float(ev.get("ts")),
                            name=_as_str(ev.get("name")),
                            cat=_as_str(ev.get("cat")),
                            dur=_as_float(ev.get("dur")),
                            args=ev.get("args") if isinstance(ev.get("args"), dict) else {},
                            raw=ev if isinstance(ev, dict) else {"value": ev},
                        )
                    )
                continue

            # Generic flat event
            events.append(
                TraceEvent(
                    ts=_as_float(obj.get("ts") or obj.get("time")),
                    name=_as_str(obj.get("name") or obj.get("event")),
                    cat=_as_str(obj.get("cat") or obj.get("category")),
                    dur=_as_float(obj.get("dur") or obj.get("duration")),
                    args=obj.get("args") if isinstance(obj.get("args"), dict) else {},
                    raw=obj,
                )
            )

        return {"events": [asdict(e) for e in events], "summary": {"count": len(events)}}

    def snapshot_config(
        self,
        *,
        include_env_keys: Optional[Iterable[str]] = None,
        max_env_values_len: int = 256
    ) -> Dict[str, Any]:
        """
        Capture a minimal, non-sensitive environment snapshot for reproducibility.
        If `include_env_keys` is provided, only those env vars are captured (value trimmed).
        Otherwise, no environment variables are included to avoid leaking secret_management.

        Returns:
            {
              "python_version": "3.x.y",
              "os": "...",
              "arch": "...",
              "env_keys": ["FOO","BAR"],
              "tool_versions": {}
            }
        """
        env_keys: List[str] = []
        env_out: Dict[str, str] = {}

        if include_env_keys:
            for k in include_env_keys:
                if k in os.environ:
                    env_keys.append(k)
                    v = os.environ.get(k, "")
                    env_out[k] = v[:max_env_values_len]

        return {
            "python_version": sys.version.split()[0],
            "os": platform.system(),
            "arch": platform.machine(),
            "env_keys": env_keys,
            "env_sample": env_out,  # small, explicit subset only if requested
            "tool_versions": {},     # reserved for future extension
        }

    # ----------------- internals -----------------

    def _iter_json_objects(self, blobs: List[Blob]) -> Iterable[Dict[str, Any]]:
        """
        Yield dict-like objects from a list of dicts / strings / JSONL.
        Non-JSON strings are wrapped as {"text": "..."} to preserve content.
        """
        if not blobs:
            return
        for b in blobs:
            try:
                if isinstance(b, dict):
                    yield b
                elif isinstance(b, str):
                    b = b.strip()
                    if not b:
                        continue
                    # JSON array
                    if b.startswith("[") and b.endswith("]"):
                        arr = json.loads(b)
                        if isinstance(arr, list):
                            for item in arr:
                                if isinstance(item, dict):
                                    yield item
                                else:
                                    yield {"value": item}
                        continue
                    # JSON object
                    if b.startswith("{") and b.endswith("}"):
                        obj = json.loads(b)
                        if isinstance(obj, dict):
                            yield obj
                            continue
                    # JSONL (try line by line)
                    parsed_any = False
                    for line in b.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        if line.startswith("{") and line.endswith("}"):
                            try:
                                obj = json.loads(line)
                                if isinstance(obj, dict):
                                    yield obj
                                    parsed_any = True
                                    continue
                            except Exception:
                                pass
                    if parsed_any:
                        continue
                    # Fallback: keep as text
                    yield {"text": b}
                else:
                    # Unknown type; wrap
                    yield {"value": b}
            except Exception as e:
                # Never raise; preserve raw info
                yield {"error": f"{type(e).__name__}: {e}"}

    def _guess_lint_tool(self, items: List[Dict[str, Any]]) -> str:
        hay = json.dumps(items, ensure_ascii=False).lower()
        if "pylint" in hay or "message-id" in hay:
            return "pylint"
        if "ruff" in hay or any(i.get("code", "").startswith("RUF") for i in items if isinstance(i, dict)):
            return "ruff"
        if any(i.get("code", "").startswith(("E", "W", "F")) for i in items if isinstance(i, dict)):
            return "flake8/pycodestyle"
        if "github" in hay and "annotation_level" in hay:
            return "github-annotations"
        return "external"

    def _guess_type_tool(self, items: List[Dict[str, Any]]) -> str:
        hay = json.dumps(items, ensure_ascii=False).lower()
        if "pyright" in hay:
            return "pyright"
        if "mypy" in hay or "mypy_path" in hay:
            return "mypy"
        return "external"

    def _map_severity(self, raw: Optional[str]) -> str:
        if not raw:
            return "warning"
        r = str(raw).strip().lower()
        if r in {"fatal", "error", "e"}:
            return "error"
        if r in {"warn", "warning", "w"}:
            return "warning"
        if r in {"info", "i", "note", "n"}:
            return "info"
        if r in {"hint", "h", "convention", "refactor", "c", "r"}:
            return "hint"
        return "warning"


# ---------- small helpers ----------

def _as_int(x: Any) -> Optional[int]:
    try:
        if x is None:
            return None
        return int(x)
    except Exception:
        return None

def _as_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None

def _as_str(x: Any) -> Optional[str]:
    try:
        if x is None:
            return None
        s = str(x)
        return s if s else None
    except Exception:
        return None

def _as_int_list(xs: Any) -> Iterable[int]:
    if isinstance(xs, (list, tuple)):
        for v in xs:
            iv = _as_int(v)
            if iv is not None:
                yield iv

def _coverage_session_to_dict(s: CoverageSession) -> Dict[str, Any]:
    return {
        "timestamp": s.timestamp,
        "totals": s.totals,
        "files": [asdict(f) for f in s.files],
        "raw": s.raw,
    }

def _weighted_lines_pct(sessions: List[CoverageSession]) -> Optional[float]:
    covered = 0
    total = 0
    for s in sessions:
        lines = s.totals.get("lines", {})
        c = _as_int(lines.get("covered"))
        m = _as_int(lines.get("missed"))
        if c is None or m is None:
            continue
        covered += c
        total += c + m
    if total == 0:
        return None
    return round((covered / total) * 100.0, 2)
