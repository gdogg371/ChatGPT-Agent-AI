#!/usr/bin/env python3
from __future__ import annotations

"""
CLI entrypoint for the patch loop (non-IDE).

This mirrors `run_patch_loop_local.py` but is intended for CLI usage (CI, scripts).
Differences are limited to defaults for output location and verbosity; logic is the same.

Features
--------
- Parameterised LLM via AskSpec (docstrings vs QA, etc).
- Platform-agnostic DB handling: NEVER auto-create. If --db-url is omitted,
  the config layer searches upward for '<repo>/databases/bot_dev.db' and requires
  it to exist (see PatchLoopConfig.normalize()).
- OpenAI API key resolution order:
    1) --api-key
    2) env OPENAI_API_KEY
    3) secret_management/secrets_loader.py (load_* / get_* / read / load)
    4) secret_management/secrets.yaml (direct read)
- Scanning constraints
    --scan-root limits lookups to a subtree (default: 'v2').
    --scan-exclude can be repeated to add extra glob exclusions; defaults remain in config.
"""

import argparse
import importlib.util
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from v2.backend.core.configuration.config import PatchLoopConfig
from v2.backend.core.prompt_pipeline.executor.engine import Engine
from v2.backend.core.prompt_pipeline.executor.errors import (
    OrchestratorError,
    PipelineError,
    AskSpecError,
)
from v2.backend.core.types.types import AskSpec


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
    Derive a sensible default project root for CLI runs.
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
        spec = importlib.util.spec_from_file_location("secrets_loader_cli", str(loader_path))
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


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the LLM patch loop (CLI).")

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
        default=default_root / "output" / "patches_cli",
        help="Base directory for run artifacts.",
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
        default=False,  # CLI default quieter; enable with --verbose
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
                "[run_patch_loop] Missing OpenAI API key.\n"
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
        print(f"[run_patch_loop] Completed. Artifacts at: {run_dir}")
        return 0

    except AskSpecError as e:
        print(f"[run_patch_loop] AskSpec error: {e}", file=sys.stderr)
        return 2
    except OrchestratorError as e:
        print(f"[run_patch_loop] Orchestrator error: {e}", file=sys.stderr)
        return 3
    except PipelineError as e:
        print(f"[run_patch_loop] Pipeline error: {e}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("[run_patch_loop] Aborted by user.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"[run_patch_loop] Unexpected error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

