# File: patches/executor/run_patch_loop_local.py
#!/usr/bin/env python3
from __future__ import annotations

"""
Local, PyCharm-friendly entrypoint for the patch loop.

This version keeps your original LLM pipeline behavior AND
plugs in the new pure-Python patch engine to apply any unified
diffs it (or you) produce. Promotion to the live mirror is
**disabled** by default.

Flow:
  1) Run the LLM pipeline (Engine) to generate patches/artifacts.
  2) Gather *.patch files (preferring output/patches_received/).
     If patches are elsewhere (e.g., output/patches_test/**),
     copy them into output/patches_received/.
  3) Apply each patch using v2.backend.core.patch_engine (pure Python).
     - Initial tests (fast) → Snapshot/Archive → Apply → Extensive tests
     - Promotion is SKIPPED unless you later change the config flag.

No DB schema changes. No Git usage for patch application.
"""

import argparse
import importlib.util
import os
import re
import sys
import json
import shutil
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Iterable

# --- Existing LLM pipeline imports (unchanged) -----------------------------------
from v2.backend.core.configuration.config import PatchLoopConfig
from v2.backend.core.prompt_pipeline.executor.engine import Engine
from v2.backend.core.prompt_pipeline.executor.errors import (
    OrchestratorError,
    PipelineError,
    AskSpecError,
)
from v2.backend.core.types.types import AskSpec

# --- Patch engine (new) ----------------------------------------------------------
from v2.backend.core.patch_engine.config import PatchEngineConfig
from v2.backend.core.patch_engine.interactive_run import run_one


# -------------------- Helpers for the existing section ---------------------------

def _find_repo_root(start: Path) -> Path:
    """
    Walk up from 'start' to filesystem root and return the first directory
    that contains 'databases/bot_dev.db'. If none is found, return CWD.
    """
    start = start.resolve()
    for p in [start, *start.parents]:
        if (p / "databases" / "bot_dev.db").is_file():
            return p
    return Path.cwd().resolve()


def _default_project_root() -> Path:
    """
    Derive a sensible default project root for local IDE runs.
    Preference: a directory that actually contains the databases/ folder.
    """
    here = Path(__file__).resolve()
    repo_root = _find_repo_root(here)
    if repo_root:
        return repo_root
    # Fallback heuristics
    for p in [here.parent, *here.parents]:
        if (p / "backend").is_dir() or (p / "v2" / "backend").is_dir():
            return p
    return Path.cwd().resolve()


def _extract_openai_key(secrets: Dict[str, Any]) -> Optional[str]:
    """
    Best-effort extraction of an OpenAI API key from a secrets dict.

    Supported shapes:
      - {"OPENAI_API_KEY": "..."}
      - {"openai": {"api_key": "..."}}
      - {"openai_api_key": "..."}
      - {"llm": {"openai": {"api_key": "..."}}}
    """
    if not isinstance(secrets, dict):
        return None

    # direct env-style
    for k in ("OPENAI_API_KEY", "openai_api_key"):
        val = secrets.get(k)
        if isinstance(val, str) and val.strip():
            return val.strip()

    # nested common shapes
    sec = secrets.get("openai")
    if isinstance(sec, dict):
        val = sec.get("api_key") or sec.get("key")
        if isinstance(val, str) and val.strip():
            return val.strip()

    llm = secrets.get("llm")
    if isinstance(llm, dict):
        sec = llm.get("openai")
        if isinstance(sec, dict):
            val = sec.get("api_key") or sec.get("key")
            if isinstance(val, str) and val.strip():
                return val.strip()

    return None


def _load_openai_api_key_from_loader(repo_root: Path) -> Optional[str]:
    """Try secret_management/secrets_loader.py via common entrypoints."""
    secrets_dir = repo_root / "secret_management"
    loader_path = secrets_dir / "secrets_loader.py"
    yaml_path = secrets_dir / "secrets.yaml"

    if not loader_path.is_file():
        return None

    try:
        spec = importlib.util.spec_from_file_location("secrets_loader_local", str(loader_path))
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[attr-defined]
    except Exception:
        return None

    candidates = ("load_secrets", "get_secrets", "load", "read")
    for fn_name in candidates:
        fn = getattr(module, fn_name, None)
        if callable(fn):
            # Try with YAML path, fallback to no-arg
            for call in ((yaml_path,), tuple()):
                try:
                    secrets = fn(*call)
                except TypeError:
                    continue
                except Exception:
                    secrets = None
                if secrets:
                    key = _extract_openai_key(secrets)  # type: ignore[arg-type]
                    if key:
                        return key

    # Fallback: module-level dict
    secrets_obj = getattr(module, "SECRETS", None)
    if isinstance(secrets_obj, dict):
        key = _extract_openai_key(secrets_obj)
        if key:
            return key

    return None


def _load_openai_api_key_from_yaml(repo_root: Path) -> Optional[str]:
    """Directly read secret_management/secrets.yaml. Uses PyYAML if present, else a tiny parser."""
    yaml_path = repo_root / "secret_management" / "secrets.yaml"
    if not yaml_path.is_file():
        return None

    # Try PyYAML first
    try:
        import yaml  # type: ignore
        with yaml_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return _extract_openai_key(data or {})
    except Exception:
        # Minimal fallback parser for simple structures:
        # openai:\n  api_key: "value"
        try:
            key: Optional[str] = None
            in_openai = False
            with yaml_path.open("r", encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith(("#", ";")):
                        continue
                    if re.match(r"^openai\s*:\s*$", line):
                        in_openai = True
                        continue
                    if in_openai:
                        m = re.match(r"^api_key\s*:\s*(.+)$", line)
                        if m:
                            val = m.group(1).strip().strip('"').strip("'")
                            if val:
                                key = val
                                break
                    # allow top-level OPENAI_API_KEY: "..."
                    m2 = re.match(r"^OPENAI_API_KEY\s*:\s*(.+)$", line)
                    if m2:
                        val = m2.group(1).strip().strip('"').strip("'")
                        if val:
                            key = val
                            break
            return key
        except Exception:
            return None


def _resolve_openai_api_key(repo_root: Path, cli_key: Optional[str]) -> Optional[str]:
    """Resolve the OpenAI API key using all supported sources."""
    # 1) CLI
    if cli_key and cli_key.strip():
        return cli_key.strip()

    # 2) Env
    env_key = os.getenv("OPENAI_API_KEY")
    if env_key and env_key.strip():
        return env_key.strip()

    # 3) Loader
    key = _load_openai_api_key_from_loader(repo_root)
    if key:
        return key

    # 4) YAML direct
    key = _load_openai_api_key_from_yaml(repo_root)
    if key:
        return key

    return None


def _build_ask_spec(
    profile: str,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    max_output_tokens: Optional[int] = None,
) -> AskSpec:
    """
    Map a human-friendly profile to an AskSpec and apply optional overrides.
    """
    prof = (profile or "").strip().lower()
    if prof in ("doc", "docstrings", "docstrings.v1"):
        spec = AskSpec.for_docstrings()
    elif prof in ("qa", "qa.default"):
        spec = AskSpec.for_qa()
    else:
        raise AskSpecError(f"Unknown ask profile: {profile!r}")

    if model is not None and model != "auto":
        spec.model = model
    if temperature is not None:
        spec.temperature = float(temperature)
    if max_output_tokens is not None:
        spec.max_output_tokens = int(max_output_tokens)

    # Optional: override response format via env (advanced)
    rf_name = os.getenv("LLM_RESPONSE_FORMAT_NAME")
    if rf_name:
        spec.response_format_name = rf_name

    spec.validate()
    return spec


# -------------------- Patch gathering helpers (new) -------------------------------

RECEIVED_DIR = Path("output/patches_received")
PATCH_SEARCH_ROOTS: List[Path] = [
    RECEIVED_DIR,                        # preferred landing zone
    Path("output/patches_test"),         # common previous location
    Path("output"),                      # broad fallback
]

def _find_patches(roots: Iterable[Path]) -> List[Path]:
    patches: List[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == ".patch":
            patches.append(root.resolve())
            continue
        if root.is_dir():
            patches.extend(sorted(root.rglob("*.patch")))
    # De-dup by path
    seen = set()
    uniq: List[Path] = []
    for p in patches:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            uniq.append(p)
    return uniq


def _ensure_in_received(patch_paths: List[Path]) -> List[Path]:
    RECEIVED_DIR.mkdir(parents=True, exist_ok=True)
    final_paths: List[Path] = []
    for p in patch_paths:
        dest = RECEIVED_DIR / p.name
        try:
            if p.resolve() != dest.resolve():
                shutil.copy2(p, dest)
                final_paths.append(dest)
            else:
                final_paths.append(p)
        except Exception:
            # If copy fails, keep original
            final_paths.append(p)
    return final_paths


def _detect_seed_dir() -> Path:
    """
    Pick a reasonable default inscope seed dir for the mirror.
    Adjust if you want to lock to 'v2/backend' or another path.
    """
    candidates = [Path("v2/backend"), Path("backend"), Path("src"), Path(".")]
    for c in candidates:
        if (c.exists() and any(c.rglob("*.py"))) or c == Path("."):
            return c.resolve()
    return Path(".").resolve()


# -------------------- Main --------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the LLM patch loop (local) and apply patches via the patch engine.")

    default_root = _default_project_root()
    parser.add_argument(
        "--project-root",
        type=Path,
        default=default_root,
        help="Project root directory (defaults to detected repo root or CWD).",
    )
    parser.add_argument(
        "--out-base",
        type=Path,
        default=default_root / "output" / "patches_test",
        help="Base directory for run artifacts (LLM pipeline).",
    )
    parser.add_argument(
        "--provider",
        choices=("openai", "mock"),
        default=os.getenv("LLM_PROVIDER", "openai"),
        help="Model provider.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=os.getenv("LLM_MODEL", "auto"),
        help="Model name (or 'auto' to let the router decide).",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=os.getenv("OPENAI_API_KEY"),
        help="API key.",
    )
    parser.add_argument(
        "--db-url",
        type=str,
        default=os.getenv("INTROSPECTION_DB_URL"),
        help=(
            "SQLAlchemy URL for introspection DB. If omitted, the config layer will "
            "search upward for <repo>/databases/bot_dev.db (must exist) without creating it."
        ),
    )
    parser.add_argument(
        "--table",
        type=str,
        default=os.getenv("INTROSPECTION_TABLE", "introspection_index"),
        help="Table name for introspection rows.",
    )
    parser.add_argument(
        "--status",
        type=str,
        default="active",
        help="Optional status filter (e.g., 'active').",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help="Limit number of rows processed.",
    )
    parser.add_argument(
        "--ask-profile",
        type=str,
        default="docstrings",
        help="Ask profile: 'docstrings' or 'qa'.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        help="Override AskSpec.temperature (0.0–2.0).",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=None,
        help="Override AskSpec.max_output_tokens.",
    )
    parser.add_argument(
        "--scan-root",
        type=str,
        default=os.getenv("SCAN_ROOT", "v2"),
        help="Repo-relative directory to constrain scanning (e.g., 'v2').",
    )
    parser.add_argument(
        "--scan-exclude",
        type=str,
        action="append",
        default=None,
        help=(
            "Glob pattern to exclude from scanning (repeatable). "
            "Defaults already exclude 'output/**', '.git/**', '__pycache__/**', '.venv/**', 'node_modules/**'."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=True,
        help="Verbose logging.",
    )
    parser.add_argument(
        "--confirm-prod-writes",
        action="store_true",
        default=False,
        help="Allow archive+replace and rollback steps to write to source files.",
    )

    args = parser.parse_args(argv)

    project_root = args.project_root.resolve()

    try:
        ask_spec = _build_ask_spec(
            profile=args.ask_profile,
            model=args.model,
            temperature=args.temperature,
            max_output_tokens=args.max_output_tokens,
        )

        # Resolve API key from CLI/env/loader/YAML
        api_key = _resolve_openai_api_key(project_root, args.api_key)

        if args.provider == "openai" and not api_key:
            secrets_yaml = project_root / "secret_management" / "secrets.yaml"
            print(
                "[run_patch_loop_local] Missing OpenAI API key.\n"
                "Tried: --api-key, env OPENAI_API_KEY, secrets_loader.py, secrets.yaml\n"
                f"Checked YAML path: {secrets_yaml}\n"
                "Please set one of the above and retry.",
                file=sys.stderr,
            )
            return 2

        # Optional overrides for scanning exclusions
        extra_cfg: Dict[str, Any] = {}
        if args.scan_exclude:
            extra_cfg["scan_exclude_globs"] = tuple(args.scan_exclude)

        # ----------------- Run the existing LLM pipeline -------------------------
        cfg = PatchLoopConfig(
            project_root=project_root,
            out_base=args.out_base,
            provider=args.provider,
            model=args.model,
            api_key=api_key,
            sqlalchemy_url=args.db_url,        # may be None; config will resolve & validate
            sqlalchemy_table=args.table,
            status_filter=args.status,
            max_rows=args.max_rows,
            verbose=bool(args.verbose),
            confirm_prod_writes=bool(args.confirm_prod_writes),
            ask_spec=ask_spec,
            scan_root=Path(args.scan_root) if args.scan_root else Path("v2"),
            **extra_cfg,
        )

        engine = Engine(cfg)
        run_dir = engine.run()
        print(f"[run_patch_loop_local] LLM pipeline complete. Artifacts at: {run_dir}")

        # ----------------- Gather and apply patches via patch engine -------------
        found = _find_patches(PATCH_SEARCH_ROOTS)
        if not found:
            print("[patch-engine] No .patch files found. Drop patches into output/patches_received/ and re-run.")
            return 0

        received = _ensure_in_received(found)
        print(f"[patch-engine] Will apply {len(received)} patch file(s) from {RECEIVED_DIR}")

        # Configure patch engine (promotion explicitly DISABLED)
        mirror_current = Path("output/mirrors/current")
        pe_cfg = PatchEngineConfig(
            mirror_current=mirror_current,
            source_seed_dir=_detect_seed_dir(),   # seed on first run
            initial_tests=[
                # Keep fast & safe; adjust to your project
                "python - <<PY\nimport compileall,sys; ok=compileall.compile_dir('.', quiet=1); sys.exit(0 if ok else 1)\nPY",
            ],
            extensive_tests=[
                "python - <<PY\nimport sys; print('sys.version=', sys.version)\nPY",
            ],
            archive_enabled=True,
            keep_last_snapshots=5,
        )
        # Do NOT promote in this runner (explicit requirement). The engine respects
        # the promotion flag inside interactive_run; leave default as disabled.

        outcomes: List[dict] = []
        for patch in received:
            print(f"[patch-engine] Applying: {patch.name}")
            manifest = run_one(patch, pe_cfg)
            outcome = manifest.data.get("outcome", {})
            outcomes.append(outcome)
            status = outcome.get("status", "unknown")
            run_id = manifest.data.get("run_id")
            print(f"[patch-engine] Outcome for {patch.name}: {status} (run_id={run_id})")

        # Optional: write a tiny session summary
        session = {"applied": len(outcomes), "outcomes": outcomes}
        (Path("output") / "runs" / "session_summary.json").write_text(
            json.dumps(session, indent=2), encoding="utf-8"
        )
        print("[patch-engine] Session summary written to output/runs/session_summary.json")
        print("[patch-engine] Done.")
        return 0

    except AskSpecError as e:
        print(f"[run_patch_loop_local] AskSpec error: {e}", file=sys.stderr)
        return 2
    except OrchestratorError as e:
        print(f"[run_patch_loop_local] Orchestrator error: {e}", file=sys.stderr)
        return 3
    except PipelineError as e:
        print(f"[run_patch_loop_local] Pipeline error: {e}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("[run_patch_loop_local] Aborted by user.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"[run_patch_loop_local] Unexpected error: {e}", file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())






