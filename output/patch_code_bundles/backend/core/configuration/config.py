from __future__ import annotations

"""
Pipeline configuration (platform-agnostic, task-agnostic).

Key points
----------
- No implicit DB creation. If sqlalchemy_url is not provided, we search upward
  from project_root for '<repo>/databases/bot_dev.db' and require it to exist.
- Generic pipeline stage flags (no docstring coupling): run_scan, run_fetch_targets, run_verify.
- Scan constraints: scan_root (default 'v2') and scan_exclude_globs (e.g., 'output/**').
- AskSpec controls LLM routing; defaults to docstrings profile but the flags are generic.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Literal

from v2.backend.core.types.types import AskSpec

ModelProvider = Literal["mock", "openai"]


def _sqlite_url_from_path(db_path: Path) -> str:
    """
    Build a platform-agnostic SQLite URL using the prototype's expected scheme.

    Examples
    --------
    Windows:   C:\\repo\\databases\\bot_dev.db    -> sqlite:///C:/repo/databases/bot_dev.db
    Linux/Mac: /home/u/repo/databases/bot_dev.db -> sqlite:////home/u/repo/databases/bot_dev.db
    """
    abs_posix = db_path.resolve().as_posix()
    return f"sqlite:///{abs_posix}"


def _find_db_upwards(start: Path, rel: Path) -> Optional[Path]:
    """Walk up from 'start' to root, returning the first existing file at 'candidate/rel'."""
    root = start.resolve()
    for p in [root, *root.parents]:
        candidate = (p / rel).resolve()
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


@dataclass(slots=True)
class PatchLoopConfig:
    # --- Project / output roots ------------------------------------------------
    project_root: Path = Path("").resolve()
    out_base: Path = Path("../../../../output/patches_received")
    run_id_suffix: Optional[str] = None

    # --- Patch artifact emission ----------------------------------------------
    save_per_item_patches: bool = True   # write one patch per DB item
    save_combined_patch: bool = True     # also write a single combined patch per source file

    # --- DB source (NO implicit creation; url may be provided externally) ------
    sqlalchemy_url: Optional[str] = None
    sqlalchemy_table: str = "introspection_index"
    status_filter: Optional[str] = "active"
    max_rows: Optional[int] = None

    # --- LLM base settings (router may override via AskSpec) -------------------
    model: str = "auto"
    provider: ModelProvider = "mock"
    api_key: Optional[str] = None
    max_output_tokens: int = 1024

    # --- Budgeting -------------------------------------------------------------
    model_ctx_tokens: int = 128_000
    response_tokens_per_item: int = 300
    budget_guardrail: int = 1024
    batch_overhead_tokens: int = 800  # account for system + schema + delimiters

    # --- Generic pipeline stage toggles ---------------------------------------
    run_scan: bool = True               # Step 1
    run_fetch_targets: bool = True      # Step 2 (formerly run_get_code_for_docstrings)
    run_build_prompts: bool = True      # Step 3
    run_run_llm: bool = True            # Step 4
    run_save_patch: bool = True         # Step 5
    run_apply_patch_sandbox: bool = True# Step 6
    run_verify: bool = True             # Step 7 (generic; adapter implements specifics)
    run_archive_and_replace: bool = True# Step 8
    run_rollback: bool = True           # Step 9

    # --- Safety gate for prod writes (affects steps 8 & 9) ---------------------
    confirm_prod_writes: bool = False

    # --- Newline handling ------------------------------------------------------
    preserve_crlf: bool = False  # if True, preserve CRLF per file when original has CRLF

    # --- Verbose console output (prompt preview) -------------------------------
    verbose: bool = True

    # --- Pipeline ask parameterisation -----------------------------------------
    ask_spec: AskSpec = field(default_factory=AskSpec.for_docstrings)

    # --- Code scanning constraints --------------------------------------------
    # Constrain all code lookups to this subtree (relative to project_root). Default: 'v2'.
    scan_root: Optional[Path] = Path("v2")
    # Paths that must never be scanned (globbed against posix-style repo-relative paths)
    scan_exclude_globs: tuple[str, ...] = (
        "output/**",
        ".git/**",
        "**/__pycache__/**",
        "**/.venv/**",
        "node_modules/**",
    )

    # Internal cache of resolved scan root (computed in normalize)
    _scan_root_abs: Optional[Path] = field(default=None, init=False, repr=False)

    # --------------------------------------------------------------------------

    def get_scan_root(self) -> Path:
        """
        Absolute scan root path computed in normalize(). If scan_root is None,
        this returns project_root.
        """
        if self._scan_root_abs is None:
            return self.project_root
        return self._scan_root_abs

    def _ensure_sqlalchemy_url(self) -> None:
        """
        Ensure sqlalchemy_url is set without causing implicit DB creation.

        Strategy:
          1) If sqlalchemy_url provided externally, keep it (trust caller).
          2) Else: search upwards from project_root for 'databases/bot_dev.db'.
             Use the first match found and build a 'sqlite:///...' URL.
          3) If none found, raise a clear error asking to run the DB init script.
        """
        if self.sqlalchemy_url:
            return

        db_rel = Path("databases") / "bot_dev.db"
        found = _find_db_upwards(self.project_root, db_rel)
        if not found:
            raise ValueError(
                "Introspection DB not found. Searched upwards from "
                f"{self.project_root} for 'databases/bot_dev.db'.\n"
                "Run your DB initialization script first (init_database), or provide "
                "--db-url / INTROSPECTION_DB_URL pointing to an existing database."
            )

        self.sqlalchemy_url = _sqlite_url_from_path(found)

    def _resolve_scan_root(self) -> None:
        """Resolve and validate the scan root subtree."""
        if self.scan_root is None:
            self._scan_root_abs = self.project_root
            return
        candidate = (self.project_root / self.scan_root).resolve()
        if not candidate.exists() or not candidate.is_dir():
            raise ValueError(
                f"Configured scan_root does not exist or is not a directory: {candidate}"
            )
        self._scan_root_abs = candidate

    def normalize(self) -> None:
        """
        Normalize paths and coerce flags. Validate ask_spec, DB configuration,
        and code scanning configuration.
        """
        self.project_root = self.project_root.resolve()
        self.out_base = self.out_base.resolve()
        self.save_per_item_patches = bool(self.save_per_item_patches)
        self.save_combined_patch = bool(self.save_combined_patch)

        # Validate ask_spec early
        try:
            if self.ask_spec is None:
                self.ask_spec = AskSpec.for_docstrings()
            self.ask_spec.validate()
        except Exception as e:
            raise ValueError(f"Invalid ask_spec configuration: {e}") from e

        # Ensure the database URL is present and points to an existing file.
        self._ensure_sqlalchemy_url()

        # Resolve scan root subtree
        self._resolve_scan_root()
