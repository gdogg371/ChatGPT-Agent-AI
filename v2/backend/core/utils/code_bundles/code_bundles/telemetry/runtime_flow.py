from __future__ import annotations

import json
import os
import sys
import time
import uuid
import hashlib
import threading
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Iterable, Union, Mapping

JsonObj = Dict[str, Any]
PathLike = Union[str, os.PathLike]


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_jsonable(obj: Any) -> Any:
    if isinstance(obj, Path):
        return str(obj)
    return obj


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=_to_jsonable)


def _ensure_parent(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: PathLike) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            h = hashlib.sha256()
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def sha256_config_dict(cfg: Mapping[str, Any]) -> str:
    """
    Canonicalize a (possibly nested) dict and hash it.
    Note: this is complementary to hashing the raw file on disk.
    """
    def canonical(o: Any) -> Any:
        if isinstance(o, Mapping):
            return {k: canonical(o[k]) for k in sorted(o.keys())}
        if isinstance(o, (list, tuple)):
            return [canonical(x) for x in o]
        if isinstance(o, Path):
            return str(o)
        return o
    can = canonical(cfg)
    return _sha256_bytes(_json_dumps(can).encode("utf-8"))


def detect_git(repo_root: Optional[PathLike] = None) -> JsonObj:
    """
    Best-effort git facts; returns {} if not a git repo or git not installed.
    """
    try:
        cwd = str(repo_root) if repo_root else None
        def run(args: Iterable[str]) -> str:
            res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
            return res.stdout.strip()
        return {
            "commit": run(["rev-parse", "HEAD"]),
            "branch": run(["rev-parse", "--abbrev-ref", "HEAD"]),
            "remote": run(["config", "--get", "remote.origin.url"]) or None,
            "is_dirty": bool(run(["status", "--porcelain"]))
        }
    except Exception:
        return {}


# ──────────────────────────────────────────────────────────────────────────────
# Config resolution (standard: packager.yml; optional env override)
# ──────────────────────────────────────────────────────────────────────────────

def _find_repo_root(start: Optional[Path] = None) -> Optional[Path]:
    """
    Walk up from 'start' (or this file / CWD) to locate a repo root that contains config/packager.yml.
    """
    candidates = []
    here = Path(__file__).resolve()
    candidates.append(here)
    candidates.append(Path.cwd().resolve())
    if start is not None:
        candidates.insert(0, start.resolve())

    seen: set[Path] = set()
    for base in candidates:
        cur = base
        # If 'base' is a file, start from its parent
        if cur.is_file():
            cur = cur.parent
        while cur and cur not in seen:
            seen.add(cur)
            cfg = cur / "config" / "packager.yml"
            if cfg.exists():
                return cur
            if cur.parent == cur:
                break
            cur = cur.parent
    return None


def _read_yaml(path: Path) -> dict:
    try:
        import yaml  # standard way in this codebase to parse packager.yml
    except Exception:
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _manifest_root_from_data(data: dict) -> Optional[Path]:
    """
    Prefer config.manifest_paths.root_dir.
    Fall back to a few legacy-ish keys if present.
    """
    # Primary, per current packager.yml layout
    mp = data.get("manifest_paths")
    if isinstance(mp, dict):
        root = mp.get("root_dir")
        if isinstance(root, str) and root.strip():
            return Path(root)

    # Secondary: a few common alternates that may exist in older configs
    for k in ("design_manifest_root", "design_manifest_dir", "design_manifest_root_dir"):
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            return Path(v)

    return None


def _manifest_root_via_build_cfg() -> Optional[Path]:
    """
    Try the project's standard config loader if import paths are set up.
    """
    try:
        # Relative import within the code_bundles package
        from ..execute.config import build_cfg  # type: ignore
        cfg = build_cfg()
        # Try the structured variant first
        mp = getattr(cfg, "manifest_paths", None)
        if isinstance(mp, dict):
            root = mp.get("root_dir")
            if isinstance(root, str) and root.strip():
                return Path(root)
        # Try common flat names on the namespace
        for k in ("design_manifest_root", "design_manifest_dir", "design_manifest_root_dir"):
            v = getattr(cfg, k, None)
            if isinstance(v, str) and v.strip():
                return Path(v)
    except Exception:
        pass
    return None


def _log_path_from_cfg() -> Path:
    """
    Resolve the default log file path:
      1) If FLOW_LOG_PATH is set, use it (env override).
      2) Else prefer packager.yml's manifest_paths.root_dir (standard).
      3) Else fall back to 'output/design_manifest'.
    """
    env_override = os.getenv("FLOW_LOG_PATH")
    if env_override:
        return Path(env_override)

    # First: try the standard loader
    root = _manifest_root_via_build_cfg()
    if root:
        return root / "run_events.jsonl"

    # Second: locate and read config/packager.yml directly
    repo_root = _find_repo_root()
    if repo_root:
        data = _read_yaml(repo_root / "config" / "packager.yml")
        root = _manifest_root_from_data(data)
        if root:
            return root / "run_events.jsonl"

    # Final fallback (kept consistent with prior behavior)
    return Path("output/design_manifest") / "run_events.jsonl"


@dataclass
class FlowEvent:
    ts: str
    run_id: str
    typ: str
    data: JsonObj = field(default_factory=dict)


class FlowLogger:
    """
    Append-only JSONL event logger for pipeline runs.
    Events:
      - run.begin
      - phase (with status, dur_ms, inputs, outputs, artifacts)
      - artifact
      - run.end
      - note (freeform)
    """
    def __init__(self,
                 log_path: Optional[PathLike] = None,
                 run_id: Optional[str] = None,
                 enabled: Optional[bool] = None):
        # Precedence: explicit arg > env > config
        eff = Path(log_path) if log_path is not None else _log_path_from_cfg()
        self.log_path = eff
        self.run_id = run_id or f"pack-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        # env override: FLOW_ENABLED=false to hard-disable
        if enabled is None:
            env = os.getenv("FLOW_ENABLED", "true").strip().lower()
            enabled = env not in ("0", "false", "no")
        self.enabled = enabled
        self._lock = threading.Lock()
        _ensure_parent(self.log_path)

    # ---------- low-level ----------
    def _write(self, ev: FlowEvent) -> None:
        if not self.enabled:
            return
        obj = {"ts": ev.ts, "run_id": ev.run_id, "type": ev.typ, **ev.data}
        line = _json_dumps(obj) + "\n"
        with self._lock, open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line)

    def _emit(self, typ: str, **data: Any) -> None:
        self._write(FlowEvent(ts=_utc_iso(), run_id=self.run_id, typ=typ, data=data))

    # ---------- public API ----------
    def begin_run(self, meta: Optional[JsonObj] = None) -> None:
        self._emit("run.begin", meta=meta or {})

    def end_run(self, status: str = "ok", meta: Optional[JsonObj] = None) -> None:
        self._emit("run.end", status=status, meta=meta or {})

    def note(self, msg: str, **fields: Any) -> None:
        self._emit("note", msg=msg, **fields)

    def artifact(self, path: PathLike, kind: Optional[str] = None, **fields: Any) -> None:
        p = Path(path)
        self._emit("artifact", path=str(p), kind=kind, exists=p.exists(), sha256=sha256_file(p), **fields)

    def phase(self, name: str, step: Optional[int] = None, **inputs: Any):
        """
        Context manager that logs a phase begin/end with duration and errors.
        Usage:
            with flow.phase("scan.ast", step=6, emit_ast=True) as ph:
                # ... do work ...
                ph.outputs(counts={"ast_symbols": 4206})
                # Example artifact path will resolve under the configured manifest root.
        """
        return _PhaseCtx(self, name=name, step=step, inputs=inputs)

    # Syntactic sugar if you want decorator style
    def phase_decorator(self, name: str, step: Optional[int] = None, **inputs: Any):
        def deco(fn):
            def wrapper(*args, **kwargs):
                with self.phase(name, step=step, **inputs) as ph:
                    res = fn(*args, **kwargs)
                    return res
            return wrapper
        return deco

    # helpers you may call from run_pack
    @staticmethod
    def default() -> "FlowLogger":
        # Precedence: env > config
        return FlowLogger(log_path=None)


class _PhaseCtx:
    def __init__(self, flow: FlowLogger, name: str, step: Optional[int], inputs: Dict[str, Any]):
        self.flow = flow
        self.name = name
        self.step = step
        self.inputs = inputs or {}
        self._outputs: Dict[str, Any] = {}
        self._artifacts: list[Dict[str, Any]] = []
        self._t0 = 0.0

    def __enter__(self) -> "_PhaseCtx":
        self._t0 = time.perf_counter()
        self.flow._emit("phase", phase=self.name, step=self.step, event="begin", inputs=self.inputs)
        return self

    def outputs(self, **kv: Any) -> None:
        # merge dicts or pass scalar fields; safe & idempotent
        for k, v in kv.items():
            if isinstance(v, Mapping) and isinstance(self._outputs.get(k), Mapping):
                d = dict(self._outputs[k])
                d.update(v)
                self._outputs[k] = d
            else:
                self._outputs[k] = v

    def artifacts(self, *paths: PathLike, kind: Optional[str] = None, **fields: Any) -> None:
        for p in paths:
            pp = Path(p)
            self._artifacts.append({
                "path": str(pp),
                "kind": kind,
                "exists": pp.exists(),
                "sha256": sha256_file(pp)
            } | fields)

    def __exit__(self, exc_type, exc, tb) -> bool:
        dur_ms = int((time.perf_counter() - self._t0) * 1000)
        status = "ok" if exc is None else "error"
        data: JsonObj = {
            "phase": self.name,
            "step": self.step,
            "event": "end",
            "status": status,
            "dur_ms": dur_ms,
            "outputs": self._outputs or {},
            "artifacts": self._artifacts or []
        }
        if exc is not None:
            data["error"] = repr(exc)
        self.flow._emit("phase", **data)
        # never swallow exceptions
        return False
