# File: scripts/run_spine_patch_loop.py
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

# --- imports from your codebase ---
try:
    from v2.backend.core.spine import Spine
    from v2.backend.core.spine.contracts import Artifact
    from v2.backend.core.configuration.spine_paths import SPINE_CAPS_PATH
except ImportError as e:
    print("ERROR: Could not import Spine modules. Ensure repo root is on PYTHONPATH.", file=sys.stderr)
    raise

# ----------------------- CLI & payload helpers -----------------------


def bool_flag(val: Any, default: bool) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    s = str(val).strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return default


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Trigger a Spine-driven, toggleable docstring patch run."
    )

    # Source / repo
    p.add_argument("--db-url", dest="DB_URL", required=True, help='e.g. "sqlite:///path/to/introspection.db"')
    p.add_argument("--table", dest="TABLE", default="introspection_index")
    p.add_argument("--status", dest="STATUS", default="active")
    p.add_argument("--max-rows", dest="MAX_ROWS", type=int, default=200)

    p.add_argument("--project-root", dest="PROJECT_ROOT", required=True)
    p.add_argument("--scan-root", dest="SCAN_ROOT")
    p.add_argument(
        "--exclude",
        dest="EXCLUDES",
        action="append",
        default=[],
        help="Glob to exclude (can be passed multiple times).",
    )
    p.add_argument("--out-base", dest="OUT_BASE", default=str(Path.cwd() / "out" / "runs"))

    # LLM
    p.add_argument("--provider", dest="PROVIDER", default=os.getenv("LLM_PROVIDER", "openai"))
    p.add_argument("--model", dest="MODEL", default=os.getenv("LLM_MODEL", "gpt-4o-mini"))
    p.add_argument("--api-key", dest="API_KEY", default=os.getenv("OPENAI_API_KEY", ""))
    p.add_argument("--temperature", dest="TEMPERATURE", type=float, default=0.2)

    # Toggles (default True; --no-* to turn off). Replace is default False (safety).
    p.add_argument("--no-fetch", dest="RUN_FETCH", action="store_false", default=True)
    p.add_argument("--no-enrich", dest="RUN_ENRICH", action="store_false", default=True)
    p.add_argument("--no-build", dest="RUN_BUILD", action="store_false", default=True)
    p.add_argument("--no-llm", dest="RUN_LLM", action="store_false", default=True)
    p.add_argument("--no-unpack", dest="RUN_UNPACK", action="store_false", default=True)
    p.add_argument("--no-write-patch", dest="RUN_WRITE_PATCH", action="store_false", default=True)
    p.add_argument("--no-sandbox", dest="RUN_SANDBOX", action="store_false", default=True)
    p.add_argument(
        "--replace",
        dest="RUN_REPLACE",
        action="store_true",
        default=False,
        help="Enable archive+replace (writes to repo).",
    )
    p.add_argument(
        "--confirm",
        dest="CONFIRM",
        action="store_true",
        default=False,
        help="Must be set with --replace to allow writes to repo.",
    )

    # Optional pipeline/profile
    p.add_argument("--pipeline-yaml", dest="pipeline_yaml", help="Absolute path to a pipeline YAML.")
    p.add_argument("--spine-profile", dest="spine_profile", help="Profile folder under spine/pipelines/")

    # Misc config
    p.add_argument("--preserve-crlf", dest="PRESERVE_CRLF", action="store_true", default=False)
    p.add_argument("--model-ctx", dest="MODEL_CTX", type=int, default=128000)
    p.add_argument("--resp-tokens-per-item", dest="RESP_TOKENS_PER_ITEM", type=int, default=320)
    p.add_argument("--batch-overhead-tokens", dest="BATCH_OVERHEAD_TOKENS", type=int, default=64)
    p.add_argument("--batch-size", dest="BATCH_SIZE", type=int, default=20)

    return p


def build_payload(args: argparse.Namespace) -> Dict[str, Any]:
    pl: Dict[str, Any] = {
        # Source / repo
        "DB_URL": args.DB_URL,
        "TABLE": args.TABLE,
        "STATUS": args.STATUS,
        "MAX_ROWS": int(args.MAX_ROWS),
        "PROJECT_ROOT": str(Path(args.PROJECT_ROOT).resolve()),
        "SCAN_ROOT": str(Path(args.SCAN_ROOT).resolve()) if args.SCAN_ROOT else str(Path(args.PROJECT_ROOT).resolve()),
        "EXCLUDES": list(args.EXCLUDES or []),
        "OUT_BASE": str(Path(args.OUT_BASE).resolve()),
        # LLM
        "PROVIDER": args.PROVIDER,
        "MODEL": args.MODEL,
        "API_KEY": args.API_KEY,
        "TEMPERATURE": float(args.TEMPERATURE),
        # Toggles
        "RUN_FETCH": bool_flag(args.RUN_FETCH, True),
        "RUN_ENRICH": bool_flag(args.RUN_ENRICH, True),
        "RUN_BUILD": bool_flag(args.RUN_BUILD, True),
        "RUN_LLM": bool_flag(args.RUN_LLM, True),
        "RUN_UNPACK": bool_flag(args.RUN_UNPACK, True),
        "RUN_WRITE_PATCH": bool_flag(args.RUN_WRITE_PATCH, True),
        "RUN_SANDBOX": bool_flag(args.RUN_SANDBOX, True),
        "RUN_REPLACE": bool_flag(args.RUN_REPLACE, False),
        "CONFIRM": bool_flag(args.CONFIRM, False),
        # Misc
        "PRESERVE_CRLF": bool_flag(args.PRESERVE_CRLF, False),
        "MODEL_CTX": int(args.MODEL_CTX),
        "RESP_TOKENS_PER_ITEM": int(args.RESP_TOKENS_PER_ITEM),
        "BATCH_OVERHEAD_TOKENS": int(args.BATCH_OVERHEAD_TOKENS),
        "BATCH_SIZE": int(args.BATCH_SIZE),
    }

    # Optional pipeline/profile
    if args.pipeline_yaml:
        pl["pipeline_yaml"] = str(Path(args.pipeline_yaml).resolve())
    if args.spine_profile:
        pl["spine_profile"] = str(args.spine_profile)

    # If OPENAI_API_KEY env exists and API_KEY is empty, use it
    if not pl.get("API_KEY") and os.getenv("OPENAI_API_KEY"):
        pl["API_KEY"] = os.getenv("OPENAI_API_KEY")

    return pl


# ------------------------------- main ---------------------------------


def print_artifacts(arts: List[Artifact]) -> int:
    """
    Print a compact summary of artifacts. Return suggested exit code:
      0 if no Problems, 1 otherwise.
    """
    problems = []
    print("\n=== Spine Run: Artifacts ===")
    for i, a in enumerate(arts, 1):
        kind = a.kind
        uri = a.uri
        print(f"{i:02d}. {kind}  {uri}")
        if kind == "Problem":
            prob = (a.meta or {}).get("problem", {})
            problems.append(f"{prob.get('code','?')}: {prob.get('message','?')}")
    if problems:
        print("\nProblems:")
        for msg in problems:
            print(f" - {msg}")
        return 1
    return 0


def main(argv: List[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)

    payload = build_payload(args)

    # Safety: warn if attempting replace without confirm
    if payload.get("RUN_REPLACE") and not payload.get("CONFIRM"):
        print("WARN: --replace requested but --confirm not set; replace step will no-op.", file=sys.stderr)

    spine = Spine(caps_path=SPINE_CAPS_PATH)
    artifacts = spine.dispatch_capability(
        capability="llm.engine.run.v1",
        payload=payload,
        intent="pipeline",
        subject=payload.get("PROJECT_ROOT") or "-",
    )

    return print_artifacts(artifacts)


if __name__ == "__main__":
    sys.exit(main())
