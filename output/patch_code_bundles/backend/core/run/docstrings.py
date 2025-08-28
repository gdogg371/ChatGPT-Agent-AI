# v2/backend/core/run/docstrings.py
from __future__ import annotations

import sys
import os
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

# We call the engine directly (no Spine orchestrator required)
from v2.backend.core.prompt_pipeline.executor.engine import run_v1 as engine_run_v1
from v2.backend.core.spine.contracts import Artifact


# --- minimal shim so the engine accepts our payload ---
class _TaskShim:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self.payload = payload
        self.envelope = {}
        self.payload_schema = {}

    def __getitem__(self, k):
        return self.payload[k]

    def get(self, k, d=None):
        return self.payload.get(k, d)


def _try_load_yaml(path: Path) -> Dict[str, Any]:
    """
    Load YAML if PyYAML is installed; otherwise error with a helpful message.
    """
    try:
        import yaml  # type: ignore
    except Exception as e:
        raise RuntimeError(
            f"PyYAML is required to read '{path}'. Install with `pip install pyyaml` "
            f"or provide DOCSTRINGS_VARS pointing to a .json file."
        ) from e
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise ValueError(f"YAML root must be a mapping (dict). Got: {type(data).__name__}")
        return data


def _load_vars_from_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(str(path))
    if path.suffix.lower() in {".json"}:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f) or {}
            if not isinstance(data, dict):
                raise ValueError(f"JSON root must be an object (dict). Got: {type(data).__name__}")
            return data
    # default: YAML
    return _try_load_yaml(path)


def _collect_variables_from_yaml() -> Dict[str, Any]:
    """
    Resolution order:
      1) env DOCSTRINGS_VARS if set (path to .yml/.yaml/.json)
      2) ./vars.yml
      3) ./vars.yaml
      4) ./run.yml
      5) ./run.yaml
    Missing file is not fatal; we return {} and rely on config loader + engine defaults.
    """
    env_path = os.environ.get("DOCSTRINGS_VARS", "").strip()
    candidates: List[Path] = []
    if env_path:
        candidates.append(Path(env_path).expanduser())
    cwd = Path.cwd()
    for name in ("vars.yml", "vars.yaml", "run.yml", "run.yaml"):
        p = cwd / name
        if p.exists():
            candidates.append(p)

    # Deduplicate while preserving order
    seen = set()
    ordered: List[Path] = []
    for p in candidates:
        key = str(p.resolve())
        if key not in seen:
            seen.add(key)
            ordered.append(p)

    for p in ordered:
        try:
            data = _load_vars_from_file(p)
            print(f"[DOCSTRINGS] loaded variables from {p}")
            return data
        except FileNotFoundError:
            continue
        except Exception as e:
            print(f"[DOCSTRINGS] warning: failed to load {p}: {e}", file=sys.stderr)
            # keep looking

    return {}


def _ensure_legacy_keys(variables: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure values the engine (and any legacy providers) expect, while keeping
    the engine resilient defaults. We *do not* force provider/model/db here;
    the engine will consult configuration.loader.
    """
    out_base = variables.get("out_base") or "output/patches_received"
    variables["out_base"] = out_base

    project_root = variables.get("project_root") or Path.cwd().as_posix()
    variables["project_root"] = project_root
    variables.setdefault("root", project_root)

    ob = Path(out_base).expanduser().resolve()
    ob.mkdir(parents=True, exist_ok=True)
    variables.setdefault("out_file", str(ob / "engine.out.json"))

    # Pass-through for sandbox mirror root (can be set in YAML)
    # If unset, the engine falls back to env PATCH_MIRROR_ROOT or temp.
    if "patch_mirror_root" in variables and variables["patch_mirror_root"]:
        variables["patch_mirror_root"] = str(Path(str(variables["patch_mirror_root"])).expanduser().resolve())

    return variables


def _render_artifacts(res: Any) -> int:
    """
    Print artifacts in a readable, deterministic way.
    Returns suggested process exit code (0 on success, 1 on problem).
    """
    artifacts: List[Any] = list(res or [])
    if not artifacts:
        print("=== Spine Run: Artifacts ===")
        print("<< no artifacts >>")
        return 1

    print("\n=== Spine Run: Artifacts ===")
    exit_code = 0
    for idx, art in enumerate(artifacts, start=1):
        # Artifacts may be v2.backend.core.spine.contracts.Artifact or plain dicts
        if isinstance(art, Artifact):
            kind = art.kind
            uri = art.uri
            meta = art.meta or {}
        elif isinstance(art, dict):
            kind = art.get("kind", "Result")
            uri = art.get("uri", "spine://shim")
            meta = art.get("meta", art)
        else:
            kind = type(art).__name__
            uri = "spine://unknown"
            meta = {}

        print(f"{idx:02d}. {kind}  {uri}")
        if kind == "Problem":
            exit_code = 1
        try:
            print(json.dumps(meta, ensure_ascii=False, indent=2))
        except Exception:
            print(str(meta))
    return exit_code


def main(argv: Optional[List[str]] = None) -> int:
    # 1) Load variables from YAML/JSON if available
    variables = _collect_variables_from_yaml()

    # 2) Ensure keys the engine/legacy providers rely on
    variables = _ensure_legacy_keys(variables or {})

    # 3) Invoke engine
    print("[DOCSTRINGS] launching engine with keys:", sorted(list(variables.keys())))
    res = engine_run_v1(_TaskShim(variables), context={})

    # 4) Render artifacts and exit
    return _render_artifacts(res)


if __name__ == "__main__":
    sys.exit(main())








