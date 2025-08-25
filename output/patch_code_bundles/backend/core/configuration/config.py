# File: v2/backend/core/configuration/config.py
from __future__ import annotations

"""
Centralized configuration objects and helpers for the patch loop & spine.

This module intentionally contains **no cross-domain imports**.
It only exposes value objects (dataclasses), defaults, and simple helpers
so any domain can import it directly without going through the spine.
"""

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union
import os
import json


# --------------------------------------------------------------------------------------
# Spine-related defaults (paths can be overridden by env or CLI)
# --------------------------------------------------------------------------------------

def _detect_repo_root(start: Optional[Path] = None) -> Path:
    start = (start or Path.cwd()).resolve()
    # Try to find a directory that contains the conventional 'databases/bot_dev.db'
    for p in [start, *start.parents]:
        if (p / "databases" / "bot_dev.db").exists():
            return p
    # Heuristics for a code repo root
    for p in [start, *start.parents]:
        if (p / "v2" / "backend").is_dir() or (p / "backend").is_dir():
            return p
    return start


def default_caps_path(project_root: Optional[Path] = None) -> Path:
    root = _detect_repo_root(project_root)
    # Conventional location for caps YAML
    candidates = [
        root / "v2" / "backend" / "core" / "spine" / "capabilities.yml",
        root / "backend" / "core" / "spine" / "capabilities.yml",
    ]
    for c in candidates:
        if c.is_file():
            return c.resolve()
    # Fall back to the first candidate path (caller may create it later)
    return candidates[0]


def default_pipelines_root(project_root: Optional[Path] = None) -> Path:
    root = _detect_repo_root(project_root)
    candidates = [
        root / "v2" / "backend" / "core" / "spine" / "pipelines",
        root / "backend" / "core" / "spine" / "pipelines",
    ]
    for c in candidates:
        if c.is_dir():
            return c.resolve()
    return candidates[0]


SPINE_CAPS_PATH: Path = Path(
    os.getenv("SPINE_CAPS_PATH", "")
).resolve() if os.getenv("SPINE_CAPS_PATH") else default_caps_path()

SPINE_PIPELINES_ROOT: Path = Path(
    os.getenv("SPINE_PIPELINES_ROOT", "")
).resolve() if os.getenv("SPINE_PIPELINES_ROOT") else default_pipelines_root()

SPINE_PROFILE: str = os.getenv("SPINE_PROFILE", "default")


# --------------------------------------------------------------------------------------
# Patch loop configuration (used by engine provider or pipeline variables)
# --------------------------------------------------------------------------------------

Jsonable = Union[None, str, int, float, bool, Dict[str, Any], List[Any]]


@dataclass
class PatchLoopConfig:
    """
    Configuration for the patch loop.

    This object is intentionally serializable and free of cross-domain imports,
    so it can be passed through the spine as a plain dict and reconstructed
    on the provider side if needed.
    """

    # Core locations
    project_root: Path
    out_base: Path

    # LLM basics
    provider: str = "openai"             # or "mock"
    model: str = "auto"
    api_key: Optional[str] = None

    # DB access
    sqlalchemy_url: Optional[str] = None
    sqlalchemy_table: str = "introspection_index"
    status_filter: Optional[str] = "active"
    max_rows: Optional[int] = None

    # Prompt/LLM budget knobs
    model_ctx_tokens: int = 16384
    response_tokens_per_item: int = 320
    batch_overhead_tokens: int = 64
    budget_guardrail: float = 0.9

    # Behaviour flags (stage gates)
    run_scan: bool = False  # external scanner is orchestrated elsewhere; keep default False here
    run_fetch_targets: bool = True
    run_build_prompts: bool = True
    run_run_llm: bool = True
    run_save_patch: bool = True
    run_apply_patch_sandbox: bool = True
    run_verify: bool = True
    run_archive_and_replace: bool = False
    run_rollback: bool = False

    # Output behaviour
    preserve_crlf: bool = True
    save_per_item_patches: bool = True
    save_combined_patch: bool = True
    run_id_suffix: Optional[str] = None

    # Logging & safety
    verbose: bool = False
    confirm_prod_writes: bool = False

    # Scan scoping
    scan_root: Path = field(default_factory=lambda: Path("v2"))
    scan_exclude_globs: Tuple[str, ...] = field(
        default_factory=lambda: (
            "**/.git/**",
            "**/.venv/**",
            "**/__pycache__/**",
            "**/node_modules/**",
            "**/output/**",
        )
    )

    # Task/LLM ask spec (kept as a dict to avoid cross-imports)
    ask_spec: Dict[str, Any] = field(default_factory=dict)

    # Arbitrary extras (forward-compat)
    extras: Dict[str, Jsonable] = field(default_factory=dict)

    # ------------------------------ Methods ---------------------------------

    def normalize(self) -> None:
        """Coerce/derive a few fields to keep downstream code simple."""
        self.project_root = Path(self.project_root).resolve()
        self.out_base = Path(self.out_base).resolve()
        self.scan_root = self._coerce_scan_root(self.scan_root)

        # Ensure out_base/run dirs exist only when used; creation handled by IO helpers.
        if self.sqlalchemy_table:
            self.sqlalchemy_table = str(self.sqlalchemy_table)

        # Ensure globs are a tuple (hashable & consistent)
        if not isinstance(self.scan_exclude_globs, tuple):
            self.scan_exclude_globs = tuple(self.scan_exclude_globs)

        # Basic sanity caps
        self.model_ctx_tokens = int(self.model_ctx_tokens or 16384)
        self.response_tokens_per_item = int(self.response_tokens_per_item or 320)
        self.batch_overhead_tokens = int(self.batch_overhead_tokens or 64)
        self.budget_guardrail = float(self.budget_guardrail or 0.9)

        if self.max_rows is not None:
            self.max_rows = int(self.max_rows)

    def get_scan_root(self) -> Path:
        """Return the effective scan root (absolute)."""
        return self._coerce_scan_root(self.scan_root)

    # ------------------------------ Serialization ---------------------------

    def to_dict(self) -> Dict[str, Any]:
        """JSON-friendly dict (paths rendered as strings)."""
        d = asdict(self)
        d["project_root"] = str(self.project_root)
        d["out_base"] = str(self.out_base)
        d["scan_root"] = str(self.scan_root)
        d["scan_exclude_globs"] = list(self.scan_exclude_globs)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PatchLoopConfig":
        """Convert back from a dict (tolerant to string paths)."""
        kwargs = dict(data)
        for key in ("project_root", "out_base", "scan_root"):
            if key in kwargs and kwargs[key] is not None:
                kwargs[key] = Path(kwargs[key])
        if "scan_exclude_globs" in kwargs and kwargs["scan_exclude_globs"] is not None:
            seq = kwargs["scan_exclude_globs"]
            if isinstance(seq, (list, tuple)):
                kwargs["scan_exclude_globs"] = tuple(str(x) for x in seq)
        return cls(**kwargs)  # type: ignore[arg-type]

    # ------------------------------ Internals --------------------------------

    def _coerce_scan_root(self, root: Union[str, Path]) -> Path:
        p = Path(root)
        if not p.is_absolute():
            try:
                return (self.project_root / p).resolve()
            except Exception:
                return p.resolve()
        return p


# --------------------------------------------------------------------------------------
# Convenience helpers for environment-driven config (optional)
# --------------------------------------------------------------------------------------

def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def load_patchloop_config_from_env(project_root: Optional[Path] = None) -> PatchLoopConfig:
    """
    Build a PatchLoopConfig from environment variables.
    Intended for simple local runs or containerized entrypoints.
    """
    root = _detect_repo_root(project_root)

    def _path_env(name: str, default: Path) -> Path:
        v = os.getenv(name)
        if not v:
            return default
        p = Path(v)
        return p if p.is_absolute() else (root / p)

    out_base = _path_env("OUT_BASE", root / "output" / "patches_env")

    # AskSpec can be passed as JSON string in ASK_SPEC
    ask_spec: Dict[str, Any] = {}
    ask_spec_raw = os.getenv("ASK_SPEC")
    if ask_spec_raw:
        try:
            ask_spec = json.loads(ask_spec_raw)
        except Exception:
            ask_spec = {}

    cfg = PatchLoopConfig(
        project_root=root,
        out_base=out_base,
        provider=os.getenv("LLM_PROVIDER", "openai"),
        model=os.getenv("LLM_MODEL", "auto"),
        api_key=os.getenv("OPENAI_API_KEY"),
        sqlalchemy_url=os.getenv("INTROSPECTION_DB_URL"),
        sqlalchemy_table=os.getenv("INTROSPECTION_TABLE", "introspection_index"),
        status_filter=os.getenv("INTROSPECTION_STATUS", "active"),
        max_rows=int(os.getenv("MAX_ROWS", "0")) or None,
        model_ctx_tokens=int(os.getenv("MODEL_CTX_TOKENS", "16384")),
        response_tokens_per_item=int(os.getenv("RESP_TOKENS_PER_ITEM", "320")),
        batch_overhead_tokens=int(os.getenv("BATCH_OVERHEAD_TOKENS", "64")),
        budget_guardrail=float(os.getenv("BUDGET_GUARDRAIL", "0.9")),
        run_scan=env_bool("RUN_SCAN", False),
        run_fetch_targets=env_bool("RUN_FETCH_TARGETS", True),
        run_build_prompts=env_bool("RUN_BUILD_PROMPTS", True),
        run_run_llm=env_bool("RUN_RUN_LLM", True),
        run_save_patch=env_bool("RUN_SAVE_PATCH", True),
        run_apply_patch_sandbox=env_bool("RUN_APPLY_PATCH_SANDBOX", True),
        run_verify=env_bool("RUN_VERIFY", True),
        run_archive_and_replace=env_bool("RUN_ARCHIVE_AND_REPLACE", False),
        run_rollback=env_bool("RUN_ROLLBACK", False),
        preserve_crlf=env_bool("PRESERVE_CRLF", True),
        save_per_item_patches=env_bool("SAVE_PER_ITEM_PATCHES", True),
        save_combined_patch=env_bool("SAVE_COMBINED_PATCH", True),
        run_id_suffix=os.getenv("RUN_ID_SUFFIX"),
        verbose=env_bool("VERBOSE", False),
        confirm_prod_writes=env_bool("CONFIRM_PROD_WRITES", False),
        scan_root=_path_env("SCAN_ROOT", Path("v2")),
        ask_spec=ask_spec,
    )
    cfg.normalize()
    return cfg

