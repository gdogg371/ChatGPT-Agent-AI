# File: v2/cli/docstring.py
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional, List, Dict, Any

# --- Spine --------------------------------------------------------------------
from v2.backend.core.spine import Spine, to_dict

# --- Use the SAME DB path as the writer/session -------------------------------
from v2.backend.core.db.access.db_init import DB_PATH  # single source of truth


# ------------------------------------------------------------------------------
# DB bootstrap stays identical to original
# ------------------------------------------------------------------------------

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


# ------------------------------------------------------------------------------
# Helpers to interact with Spine
# ------------------------------------------------------------------------------

def _default_caps_path(project_root: Path) -> Path:
    # Conventional location for capability map
    return project_root / "v2" / "backend" / "core" / "spine" / "capabilities.yml"


def _artifact_problem_summary(arts) -> str:
    msgs: List[str] = []
    for a in arts:
        if getattr(a, "kind", None) == "Problem":
            p = (a.meta or {}).get("problem", {})
            msgs.append(f"{p.get('code','Problem')}: {p.get('message','(no message)')}")
    return "; ".join(msgs) if msgs else ""


def _extract_result(arts):
    if len(arts) == 1 and isinstance(arts[0].meta, dict) and "result" in arts[0].meta:
        return arts[0].meta["result"]
    return [to_dict(a) for a in arts]


# ------------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="End-to-end: scan docstrings -> populate DB -> run LLM patch loop (via Spine)."
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
        help="Where to store LLM artifacts & patches.",
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
        help="introspection_index.status filter used downstream.",
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
        help="Allow archive/replace operations during patch application.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=True,
        help="Verbose logging from the LLM pipeline.",
    )

    # New (Spine-specific, with sane defaults)
    parser.add_argument(
        "--caps-path",
        type=Path,
        default=None,
        help="Path to backend/core/spine/capabilities.yml (defaults to repo conventional path).",
    )

    args = parser.parse_args(argv)

    project_root: Path = args.project_root.resolve()
    scan_root: Path = args.scan_root.resolve()
    out_base: Path = args.out_base.resolve()
    caps_path: Path = (args.caps_path or _default_caps_path(project_root)).resolve()

    # 0) Init schema in the SAME DB file the writer/session uses
    _init_db_at_writer_path(project_root)

    # 1) Scan & (optionally) persist to DB via Spine
    print(f("[e2e] Scanning docstrings under: {scan_root}"))
    _ensure_env_for_scanner(scan_root)

    spine = Spine(caps_path=caps_path)

    # Step 1: scan
    scan_arts = spine.dispatch_capability(
        capability="docstrings.scan.python.v1",
        payload={
            "roots": [str(scan_root)],
            "includes": ["**/*.py"],
            "excludes": ["**/__pycache__/**", "**/.venv/**", "**/tests/**"],
        },
        intent="discover",
        subject=str(scan_root),
        context={"cli": "docstring"},
    )
    if any(a.kind == "Problem" for a in scan_arts):
        print("[e2e] Scan failed:", _artifact_problem_summary(scan_arts), file=sys.stderr)
        return 2

    scan_result = _extract_result(scan_arts)
    stats = {}
    if isinstance(scan_result, dict):
        stats = scan_result.get("stats", {}) or {}
    print(
        f"[e2e] Scan done — files:{stats.get('files') or stats.get('total_files')} "
        f"records:{stats.get('records')}"
    )

    # Step 2: build prompts
    prompts_arts = spine.dispatch_capability(
        capability="prompts.build.v1",
        payload={
            "batch": scan_result,  # baton from scan
            "style": "pep257",
            "model": args.model,
        },
        intent="plan",
        subject=str(scan_root),
        context={"cli": "docstring"},
    )
    if any(a.kind == "Problem" for a in prompts_arts):
        print("[e2e] Prompt build failed:", _artifact_problem_summary(prompts_arts), file=sys.stderr)
        return 3
    prompts_result = _extract_result(prompts_arts)

    # Step 3: write docstring batch to DB (enterprise-style profile)
    write_arts = spine.dispatch_capability(
        capability="introspection.write.v1",
        payload={
            "db_path": str(DB_PATH),
            "table": "introspection_index",
            "if_exists": "append",
            "records": scan_result,  # persist original scan batch
        },
        intent="analyze",
        subject=str(DB_PATH),
        context={"cli": "docstring"},
    )
    if any(a.kind == "Problem" for a in write_arts):
        print("[e2e] DB write failed:", _artifact_problem_summary(write_arts), file=sys.stderr)
        return 4

    # 2) Patch pipeline, pointing at the SAME DB file
    db_url = _sqlite_url_from_dbpath()
    table = "introspection_index"
    rp = project_root
    try:
        scan_root_arg = scan_root.relative_to(rp).as_posix()
    except Exception:
        scan_root_arg = str(scan_root)

    # Step 4: inject prompts into bundle
    inject_arts = spine.dispatch_capability(
        capability="codebundle.inject_prompts.v1",
        payload={
            "prompts": prompts_result,
            "bundle_root": str(out_base),
            "manifest_relpath": "design_manifest/prompts.jsonl",
        },
        intent="plan",
        subject=str(out_base),
        context={"cli": "docstring"},
    )
    if any(a.kind == "Problem" for a in inject_arts):
        print("[e2e] Prompt injection failed:", _artifact_problem_summary(inject_arts), file=sys.stderr)
        return 5

    # Step 5: run patch engine via spine (capability should wrap the legacy runner)
    patch_arts = spine.dispatch_capability(
        capability="patch.run.v1",
        payload={
            "prompts": _extract_result(inject_arts),
            "provider": args.provider,
            "model": args.model,
            "api_key": args.api_key or "",
            "db_url": db_url,
            "table": table,
            "status": args.status,
            "scan_root": scan_root_arg,
            "project_root": str(rp),
            "out_base": str(out_base),
            "max_rows": args.max_rows,
            "confirm_prod_writes": bool(args.confirm_prod_writes),
            "verbose": bool(args.verbose),
        },
        intent="patch",
        subject=str(rp),
        context={"cli": "docstring"},
    )

    if any(a.kind == "Problem" for a in patch_arts):
        print("[e2e] Patch loop failed:", _artifact_problem_summary(patch_arts), file=sys.stderr)
        return 6

    print("[e2e] Patch loop completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())




