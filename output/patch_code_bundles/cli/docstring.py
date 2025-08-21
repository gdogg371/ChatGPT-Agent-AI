# File: cli/e2e_docstrings.py
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional, List

# --- Docstring scan -> DB ------------------------------------------------------
from v2.backend.core.introspect.read_docstrings import DocStringAnalyzer

# --- LLM pipeline + patch application -----------------------------------------
from v2.patches.executor import run_patch_loop_local

# --- Use the SAME DB path as the writer/session -------------------------------
from v2.backend.core.db.access.db_init import DB_PATH  # <-- single source of truth


def _init_db_at_writer_path(project_root: Path) -> None:
    """
    Initialize the SQLite schema in the exact DB file that the ORM/writer uses.
    We monkey-patch the constants inside init_sqlite_dev to avoid drift.
    """
    import v2.backend.core.utils.db.init_sqlite_dev as initdev

    target_db = Path(DB_PATH)
    target_db.parent.mkdir(parents=True, exist_ok=True)

    # Point the initializer at the SAME file and real schema dir
    initdev.DB_PATH = str(target_db)
    initdev.BASE_DIR = str(target_db.parent)
    initdev.SCHEMA_DIR = str((project_root / "scripts" / "sqlite_sql_schemas").resolve())

    print("[e2e] Initializing SQLite database & required tables…")
    initdev.init_database()


def _ensure_env_for_scanner(scan_root: Path) -> None:
    os.environ["DOCSTRING_ROOT"] = str(scan_root.resolve())


def _sqlite_url_from_dbpath() -> str:
    # The engine in db_init.py uses sqlite:///{DB_PATH}
    return f"sqlite:///{DB_PATH}"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="End-to-end: scan docstrings -> populate DB -> run LLM patch loop."
    )
    cwd = Path.cwd().resolve()

    parser.add_argument(
        "--project-root",
        type=Path,
        default=cwd,
        help="Repo root (used by the LLM pipeline for run dirs and scan scoping).",
    )
    parser.add_argument(
        "--scan-root",
        type=Path,
        default=(cwd / "v2"),
        help="Directory to scan for Python files/docstrings. Often repo_root/v2.",
    )
    parser.add_argument(
        "--out-base",
        type=Path,
        default=(cwd / "output" / "patches_test"),
        help="Where to store LLM artifacts & patches (same shape as run_patch_loop_local).",
    )
    parser.add_argument(
        "--provider",
        choices=("openai", "mock"),
        default=os.getenv("LLM_PROVIDER", "openai"),
        help="Model provider for the LLM pipeline.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=os.getenv("LLM_MODEL", "auto"),
        help="Model name (or 'auto' for router).",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=os.getenv("OPENAI_API_KEY"),
        help="OpenAI API key (if provider=openai).",
    )
    parser.add_argument(
        "--status",
        type=str,
        default="active",
        help="introspection_index.status filter the pipeline uses.",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help="Optional limit on rows pulled from DB for the LLM step.",
    )
    parser.add_argument(
        "--confirm-prod-writes",
        action="store_true",
        default=False,
        help="Pass through to patch loop to allow archive/replace.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=True,
        help="Verbose logging from the LLM pipeline.",
    )

    args = parser.parse_args(argv)

    project_root: Path = args.project_root.resolve()
    scan_root: Path = args.scan_root.resolve()
    out_base: Path = args.out_base.resolve()

    # 0) Init schema in the SAME DB file the writer/session uses
    _init_db_at_writer_path(project_root)

    # 1) Scan & populate DB
    print(f"[e2e] Scanning docstrings under: {scan_root}")
    _ensure_env_for_scanner(scan_root)
    analyzer = DocStringAnalyzer()
    stats = analyzer.traverse_and_write()
    print(
        f"[e2e] Scan done — files:{stats['total_files']} written:{stats['total_written']} "
        f"skipped:{stats['total_skipped']} failed:{stats['total_failed']} llm:{stats['total_llm']}"
    )

    # 2) Run the existing LLM+patch loop, pointing at the SAME DB file
    db_url = _sqlite_url_from_dbpath()
    table = "introspection_index"

    rp = project_root
    try:
        scan_root_arg = scan_root.relative_to(rp).as_posix()
    except Exception:
        scan_root_arg = str(scan_root)

    patch_argv = [
        f"--project-root={str(rp)}",
        f"--out-base={str(out_base)}",
        f"--provider={args.provider}",
        f"--model={args.model}",
        f"--api-key={args.api_key or ''}",
        f"--db-url={db_url}",
        f"--table={table}",
        f"--status={args.status}",
        f"--scan-root={scan_root_arg}",
    ]
    if args.max_rows is not None:
        patch_argv.append(f"--max-rows={int(args.max_rows)}")
    if args.confirm_prod_writes:
        patch_argv.append("--confirm-prod-writes")
    if args.verbose:
        patch_argv.append("--verbose")

    print("[e2e] Launching LLM pipeline + patch engine…")
    rc = run_patch_loop_local.main(patch_argv)
    if rc != 0:
        print(f"[e2e] Patch loop exited with code {rc}", file=sys.stderr)
    else:
        print("[e2e] Patch loop completed.")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())


