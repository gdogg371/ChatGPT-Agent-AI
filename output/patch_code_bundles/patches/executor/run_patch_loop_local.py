# File: patches/executor/run_patch_loop_local.py
#!/usr/bin/env python3
from __future__ import annotations
"""
Local, PyCharm-friendly entrypoint for the patch loop — now **class-based** and
with **granular stage controls**. It preserves existing behaviour and wiring,
adds safe defaults, fixes path issues (absolute mirror path), and emits optional
events to your `\spine` layer if present (no hard dependency).

Flow (configurable by flags):
  1) Read targets from DB (introspection_index)               [--no-fetch]
  2) Build prompts / packs                                    [--no-build]
  3) Call LLM via router/profile                              [--no-llm]
  4) Write per-item & combined patches                        [--no-save]
  5) Apply to sandbox (in run dir)                            [--no-sandbox]
  6) Verify (adapter-specific checks)                         [--no-verify]
  7) Archive & replace (requires --confirm-prod-writes)       [--no-archive]
  8) Rollback placeholder                                     [--no-rollback]
  9) (Controller) Apply *.patch via pure-Python patch engine  [--no-apply]

No DB schema changes. No Git usage for patch application.
"""

import argparse
import importlib.util
import os
import re
import sys
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Iterable, Tuple

# --- Existing LLM pipeline imports (unchanged) ---------------------------------
from v2.backend.core.configuration.config import PatchLoopConfig
from v2.backend.core.prompt_pipeline.executor.engine import Engine
from v2.backend.core.prompt_pipeline.executor.errors import AskSpecError
from v2.backend.core.types.types import AskSpec

# --- Patch engine (pure-Python) ------------------------------------------------
from v2.backend.core.patch_engine.config import PatchEngineConfig
from v2.backend.core.patch_engine.interactive_run import run_one

# --- Optional: spine bus (non-fatal if missing) --------------------------------
def _try_get_spine():
    """
    Best-effort import for a project-local spine/transport layer.
    Accepts either:
      - from v2.spine import spine
      - from v2.spine.bus import spine
      - from v2.spine.bus import Spine; spine = Spine.global_bus()
    Returns (spine_object_or_None, how:str).
    """
    candidates: Tuple[Tuple[str, str], ...] = (
        ("v2.spine", "spine"),
        ("v2.spine.bus", "spine"),
        ("v2.spine.bus", "Spine"),
    )
    for mod, attr in candidates:
        try:
            m = __import__(mod, fromlist=[attr])
            obj = getattr(m, attr, None)
            if obj is None:
                continue
            # Handle class case
            if callable(obj) and obj.__name__.lower().startswith("spine"):
                try:
                    inst = obj.global_bus()  # type: ignore[attr-defined]
                except Exception:
                    try:
                        inst = obj()  # type: ignore[call-arg]
                    except Exception:
                        continue
                return inst, f"{mod}:{attr}()"
            return obj, f"{mod}:{attr}"
        except Exception:
            continue
    return None, "not found"


# -------------------- Helpers (OpenAI key resolution) --------------------------
def _find_repo_root(start: Path) -> Path:
    """
    Walk up from 'start' to filesystem root and return the first directory that
    contains 'databases/bot_dev.db'. If none is found, return CWD.
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
    for p in [here.parent, *here.parents]:
        if (p / "backend").is_dir() or (p / "v2" / "backend").is_dir():
            return p
    return Path.cwd().resolve()


def _extract_openai_key(secrets: Dict[str, Any]) -> Optional[str]:
    """Best-effort extraction of an OpenAI API key from a secrets dict."""
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
    """Resolve the OpenAI API key using CLI/env/loader/YAML in that order."""
    if cli_key and cli_key.strip():
        return cli_key.strip()
    env_key = os.getenv("OPENAI_API_KEY")
    if env_key and env_key.strip():
        return env_key.strip()
    key = _load_openai_api_key_from_loader(repo_root)
    if key:
        return key
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
    """Map a human-friendly profile to an AskSpec and apply optional overrides."""
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

    rf_name = os.getenv("LLM_RESPONSE_FORMAT_NAME")
    if rf_name:
        spec.response_format_name = rf_name

    spec.validate()
    return spec


# -------------------- Patch gathering helpers ----------------------------------
RECEIVED_DIR = Path("output/patches_received")
PATCH_SEARCH_ROOTS: List[Path] = [
    RECEIVED_DIR,                 # preferred landing zone
    Path("output/patches_test"),  # common previous location
    Path("output"),               # broad fallback
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


def _detect_seed_dir(project_root: Path) -> Path:
    """
    Pick a reasonable default inscope seed dir for the mirror, under project_root.
    Preference order: v2/backend → backend → src → project_root.
    """
    candidates = [project_root / "v2" / "backend", project_root / "backend", project_root / "src", project_root]
    for c in candidates:
        try:
            if (c.exists() and any(c.rglob("*.py"))) or c == project_root:
                return c.resolve()
        except Exception:
            continue
    return project_root.resolve()


# -------------------- Controller ------------------------------------------------
class PatchLoopLocalController:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.project_root: Path = args.project_root.resolve()
        self.out_base: Path = args.out_base.resolve()
        self.spine, self._spine_how = _try_get_spine()

    # ---- Build the engine config from CLI flags (maps stage controls) ----
    def build_engine_config(self) -> PatchLoopConfig:
        extra_cfg: Dict[str, Any] = {}
        if self.args.scan_exclude:
            extra_cfg["scan_exclude_globs"] = tuple(self.args.scan_exclude)

        # Stage switches (set to False when --no-xxx passed)
        stage_overrides: Dict[str, Any] = dict(
            run_fetch_targets=not self.args.no_fetch,
            run_build_prompts=not self.args.no_build,
            run_run_llm=not self.args.no_llm,
            run_save_patch=not self.args.no_save,
            run_apply_patch_sandbox=not self.args.no_sandbox,
            run_verify=not self.args.no_verify,
            run_archive_and_replace=not self.args.no_archive,
            run_rollback=not self.args.no_rollback,
            # run_scan is a no-op placeholder in Engine; keep default True
        )

        cfg = PatchLoopConfig(
            project_root=self.project_root,
            out_base=self.out_base,
            provider=self.args.provider,
            model=self.args.model,
            api_key=self.args.api_key_resolved,  # resolved earlier
            sqlalchemy_url=self.args.db_url,
            sqlalchemy_table=self.args.table,
            status_filter=self.args.status,
            max_rows=self.args.max_rows,
            verbose=bool(self.args.verbose),
            confirm_prod_writes=bool(self.args.confirm_prod_writes),
            ask_spec=self.args.ask_spec,  # built earlier
            scan_root=Path(self.args.scan_root) if self.args.scan_root else Path("v2"),
            **stage_overrides,
            **extra_cfg,
        )
        return cfg

    # ---- Run the LLM pipeline (spine-first, fallback to direct) ----
    def run_engine(self, cfg: PatchLoopConfig) -> Path:
        if self.spine:
            try:
                payload = {
                    "project_root": str(cfg.project_root),
                    "out_base": str(cfg.out_base),
                    "provider": cfg.provider,
                    "model": cfg.model,
                    "api_key": cfg.api_key,
                    "sqlalchemy_url": cfg.sqlalchemy_url,
                    "sqlalchemy_table": cfg.sqlalchemy_table,
                    "status_filter": cfg.status_filter,
                    "max_rows": cfg.max_rows,
                    "verbose": cfg.verbose,
                    "confirm_prod_writes": cfg.confirm_prod_writes,
                    "ask_spec": cfg.ask_spec.to_dict(),  # type: ignore[attr-defined]
                    "scan_root": str(cfg.get_scan_root()),
                    # stage flags:
                    "run_scan": cfg.run_scan,
                    "run_fetch_targets": cfg.run_fetch_targets,
                    "run_build_prompts": cfg.run_build_prompts,
                    "run_run_llm": cfg.run_run_llm,
                    "run_save_patch": cfg.run_save_patch,
                    "run_apply_patch_sandbox": cfg.run_apply_patch_sandbox,
                    "run_verify": cfg.run_verify,
                    "run_archive_and_replace": cfg.run_archive_and_replace,
                    "run_rollback": cfg.run_rollback,
                }
                # Request/response API if supported by your spine
                if hasattr(self.spine, "request"):
                    resp = self.spine.request("engine.run", payload, timeout=300)  # type: ignore[attr-defined]
                    run_dir = Path(resp.get("run_dir") or self.out_base).resolve()
                    return run_dir
                # Fallback: publish-only
                if hasattr(self.spine, "publish"):
                    self.spine.publish("engine.run", payload)  # type: ignore[attr-defined]
            except Exception:
                # Soft-fail to direct call
                pass

        # Direct call fallback (original behaviour)
        engine = Engine(cfg)
        return engine.run()

    # ---- Collect patch files from known locations ----
    def gather_patches(self, run_dir: Path) -> List[Path]:
        # Prefer anything the engine wrote under run_dir
        candidates: List[Path] = []
        if run_dir and run_dir.exists():
            candidates.extend(sorted(run_dir.rglob("*.patch")))
        # Also look in the usual drop-zones
        found = _find_patches(PATCH_SEARCH_ROOTS)
        # Merge & de-dup
        seen: set[Path] = set()
        uniq: List[Path] = []
        for p in [*candidates, *found]:
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                uniq.append(p)
        # Normalize to output/patches_received
        return _ensure_in_received(uniq)

    # ---- Apply patches with the pure-Python engine ----
    def apply_patches(self, patches: List[Path]) -> int:
        if not patches:
            print("[patch-engine] No .patch files found. Drop patches into output/patches_received/ and re-run.")
            return 0

        mirror_current = (self.project_root / "output" / "mirrors" / "current").resolve()
        pe_cfg = PatchEngineConfig(
            mirror_current=mirror_current,
            source_seed_dir=_detect_seed_dir(self.project_root),
            # Keep fast & safe defaults; adjust as needed for your project
            initial_tests=[
                # simple syntax/import checks; keep these lightweight
                "python - << \"import sys; print(sys.version.split()[0])\"",
            ],
            extensive_tests=[
                # extend with unit tests or sanity scripts if you have them
            ],
            excludes=[
                "**/.git/**",
                "**/.venv/**",
                "**/__pycache__/**",
                "**/output/**",
            ],
            promotion_enabled=False,  # explicit
        )

        # Optional: emit spine events
        if self.spine and hasattr(self.spine, "publish"):
            try:
                self.spine.publish("patch.apply.started", {"count": len(patches), "mirror": str(mirror_current)})
            except Exception:
                pass

        # Apply each patch
        failures = 0
        for patch in patches:
            try:
                manifest = run_one(patch, pe_cfg)
                outcome = (manifest.as_dict() if hasattr(manifest, "as_dict") else {})  # type: ignore[attr-defined]
                status = (outcome.get("outcome") or {}).get("status", "unknown")
                print(f"[patch-engine] {patch.name}: {status}")
                if status not in ("promoted", "would_promote_but_disabled", "extensive_tests_failed", "initial_tests_failed"):
                    # Treat apply failures & scope rejections as failures
                    if status not in ("promoted", "would_promote_but_disabled"):
                        # apply_failed / rejected_* considered failure
                        if status != "would_promote_but_disabled":
                            failures += 1
            except Exception as e:
                print(f"[patch-engine] ERROR applying {patch}: {e}", file=sys.stderr)
                failures += 1

        if self.spine and hasattr(self.spine, "publish"):
            try:
                self.spine.publish("patch.apply.completed", {"failures": failures, "total": len(patches)})
            except Exception:
                pass

        return 1 if failures else 0

    # ---- Orchestrate all stages based on flags ----
    def run(self) -> int:
        # 1) Build config for the Engine
        cfg = self.build_engine_config()

        # 2) Optionally run DB->LLM pipeline (skip if user asked to only-apply)
        run_dir = Path("")
        if not self.args.only_apply:
            run_dir = self.run_engine(cfg)
            print(f"[run_patch_loop_local] LLM pipeline complete. Artifacts at: {run_dir}")

        # 3) Optionally apply patches (skip with --no-apply)
        if self.args.no_apply:
            return 0

        received = self.gather_patches(run_dir)
        print(f"[patch-engine] Will apply {len(received)} patch file(s) from {RECEIVED_DIR}")
        return self.apply_patches(received)


# -------------------- CLI -------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the LLM patch loop (local) with granular stage controls and apply patches via the patch engine."
    )

    default_root = _default_project_root()

    # General
    parser.add_argument(
        "--project-root", type=Path, default=default_root,
        help="Project root directory (defaults to detected repo root or CWD).",
    )
    parser.add_argument(
        "--out-base", type=Path, default=default_root / "output" / "patches_test",
        help="Base directory for run artifacts (LLM pipeline).",
    )

    # Model / provider
    parser.add_argument("--provider", choices=("openai", "mock"), default=os.getenv("LLM_PROVIDER", "openai"))
    parser.add_argument("--model", type=str, default=os.getenv("LLM_MODEL", "auto"))
    parser.add_argument("--api-key", type=str, default=os.getenv("OPENAI_API_KEY"))

    # DB
    parser.add_argument(
        "--db-url", type=str, default=os.getenv("INTROSPECTION_DB_URL"),
        help=(
            "SQLAlchemy URL for introspection DB. If omitted, the config layer will search "
            "upward for /databases/bot_dev.db and require it to exist."
        ),
    )
    parser.add_argument("--table", type=str, default=os.getenv("INTROSPECTION_TABLE", "introspection_index"))
    parser.add_argument("--status", type=str, default="active")
    parser.add_argument("--max-rows", type=int, default=None)

    # Ask profile & overrides
    parser.add_argument("--ask-profile", type=str, default="docstrings")
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--max-output-tokens", type=int, default=None)

    # Scan constraints
    parser.add_argument("--scan-root", type=str, default=os.getenv("SCAN_ROOT", "v2"))
    parser.add_argument("--scan-exclude", type=str, action="append", default=None)

    # Verbosity & write safety
    parser.add_argument("--verbose", action="store_true", default=True)
    parser.add_argument("--confirm-prod-writes", action="store_true", default=False)

    # ---- New granular stage controls (Engine) ----
    parser.add_argument("--no-fetch", action="store_true", help="Skip Step 2: read rows from DB.")
    parser.add_argument("--no-build", action="store_true", help="Skip Step 3: build prompts/packs.")
    parser.add_argument("--no-llm", action="store_true", help="Skip Step 4: call LLM.")
    parser.add_argument("--no-save", action="store_true", help="Skip Step 5: write patches.")
    parser.add_argument("--no-sandbox", action="store_true", help="Skip Step 6: apply to sandbox.")
    parser.add_argument("--no-verify", action="store_true", help="Skip Step 7: run adapter verify.")
    parser.add_argument("--no-archive", action="store_true", help="Skip Step 8: archive & replace.")
    parser.add_argument("--no-rollback", action="store_true", help="Skip Step 9: rollback placeholder.")

    # ---- New controller switches (outside Engine) ----
    parser.add_argument("--no-apply", action="store_true", help="Do not invoke the patch engine at all.")
    parser.add_argument("--only-apply", action="store_true", help="Skip LLM pipeline, only apply existing *.patch files.")

    args = parser.parse_args(argv)

    # Resolve project root now to keep all paths absolute downstream
    args.project_root = args.project_root.resolve()
    # Build AskSpec and resolve API key before handing to controller
    try:
        args.ask_spec = _build_ask_spec(
            profile=args.ask_profile,
            model=args.model,
            temperature=args.temperature,
            max_output_tokens=args.max_output_tokens,
        )
    except AskSpecError as e:
        print(f"[run_patch_loop_local] AskSpec error: {e}", file=sys.stderr)
        return 2

    args.api_key_resolved = _resolve_openai_api_key(args.project_root, args.api_key)
    if args.provider == "openai" and not args.api_key_resolved:
        secrets_yaml = args.project_root / "secret_management" / "secrets.yaml"
        print(
            "[run_patch_loop_local] Missing OpenAI API key.\n"
            "Tried: --api-key, env OPENAI_API_KEY, secrets_loader.py, secrets.yaml\n"
            f"Checked YAML path: {secrets_yaml}\n"
            "Please set one of the above and retry.",
            file=sys.stderr,
        )
        return 2

    controller = PatchLoopLocalController(args)
    return controller.run()


if __name__ == "__main__":
    raise SystemExit(main())

