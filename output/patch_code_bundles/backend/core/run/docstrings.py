# v2/backend/core/run/docstrings.py
from __future__ import annotations

import sys
import os
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

# Engine entrypoint (generic; no domain specifics here)
from v2.backend.core.prompt_pipeline.executor.engine import run_v1 as engine_run_v1

# Registry/loader (for YAML-driven capability wiring)
from v2.backend.core.spine.loader import CapabilitiesLoader, REGISTRY

# Artifacts shim (for nice printing of Artifact lists, if any)
from v2.backend.core.spine.contracts import Artifact  # type: ignore


# ---- Task adapter -------------------------------------------------------------

class _TaskShim:
    """
    Minimal shim so the engine accepts a Task-like object with .payload.
    """
    def __init__(self, payload: Dict[str, Any]) -> None:
        self.payload = payload
        self.envelope = {}
        self.payload_schema = {}

    def __getitem__(self, k):
        return self.payload[k]

    def get(self, k, d=None):
        return self.payload.get(k, d)


# ---- Config / variables loaders ----------------------------------------------

def _try_load_yaml(path: Path) -> Dict[str, Any]:
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
    if path.suffix.lower() == ".json":
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f) or {}
            if not isinstance(data, dict):
                raise ValueError(f"JSON root must be an object (dict). Got: {type(data).__name__}")
            return data
    return _try_load_yaml(path)


def _collect_variables_from_yaml() -> Dict[str, Any]:
    """
    Resolution order:
      1) env DOCSTRINGS_VARS (path to .yml/.yaml/.json)
      2) ./vars.yml / ./vars.yaml
      3) ./run.yml  / ./run.yaml
    Missing file is fine → {} (engine defaults will apply).
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

    seen: set[str] = set()
    for p in candidates:
        key = str(p.resolve())
        if key in seen:
            continue
        seen.add(key)
        try:
            data = _load_vars_from_file(p)
            print(f"[DOCSTRINGS] loaded variables from {p}")
            return data
        except FileNotFoundError:
            continue
        except Exception as e:
            print(f"[DOCSTRINGS] warning: failed to load {p}: {e}", file=sys.stderr)

    return {}


def _ensure_engine_keys(variables: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure keys the engine expects while keeping everything generic.
    """
    out = dict(variables or {})

    out_base = out.get("out_base") or "output/patches_received"
    out["out_base"] = out_base

    project_root = out.get("project_root") or Path.cwd().as_posix()
    out["project_root"] = project_root
    out.setdefault("root", project_root)

    ob = Path(out_base).expanduser().resolve()
    ob.mkdir(parents=True, exist_ok=True)
    out.setdefault("out_file", str(ob / "engine.out.json"))

    # Optional mirror target for apply phase
    if out.get("patch_mirror_root"):
        out["patch_target_root"] = str(Path(str(out["patch_mirror_root"])).expanduser().resolve())

    return out


# ---- Capability map loader (YAML-driven; no direct imports) -------------------

def _load_capabilities_yaml() -> None:
    """
    Load capability→provider bindings into the registry.

    Resolution order:
      1) env DOCSTRINGS_CAPS (absolute or relative path)
      2) <cwd>/config/spine/capabilities.yml
      3) Walk upwards from this file to find ../../../../config/spine/capabilities.yml
    """
    env_caps = os.environ.get("DOCSTRINGS_CAPS", "").strip()

    candidates: List[Path] = []
    if env_caps:
        candidates.append(Path(env_caps).expanduser().resolve())

    # project-root default: ./config/spine/capabilities.yml
    candidates.append(Path.cwd() / "config" / "spine" / "capabilities.yml")

    # walk up from this file to find a top-level /config/spine/capabilities.yml
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        candidates.append(parent / "config" / "spine" / "capabilities.yml")

    caps_path = next((p for p in candidates if p.exists()), None)
    if not caps_path:
        raise FileNotFoundError(
            "capabilities file not found; tried (in order):\n  - "
            + "\n  - ".join(str(p) for p in candidates)
            + "\nSet DOCSTRINGS_CAPS to an explicit path if needed."
        )

    CapabilitiesLoader(caps_path).load(REGISTRY)
    print(f"[DOCSTRINGS] loaded capabilities from {caps_path}")


# ---- Artifact rendering -------------------------------------------------------

def _render_artifacts(res: Any) -> int:
    """
    Render result/arts; return 0 on success, 1 on problem.
    """
    # Engine returns a dict (generic result)
    if isinstance(res, dict):
        print("\n=== Spine Run: Result ===")
        try:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        except Exception:
            print(str(res))
        return 0

    # Fallback: list of artifacts
    artifacts: List[Any] = list(res or [])
    print("\n=== Spine Run: Artifacts ===")
    if not artifacts:
        print("<< no artifacts >>")
        return 1

    exit_code = 0
    for idx, art in enumerate(artifacts, start=1):
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


# ---- Main --------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    # 1) Load capability bindings (YAML-driven, generic)
    _load_capabilities_yaml()

    # 2) Load variables (optional user overrides)
    variables = _collect_variables_from_yaml()

    # 3) Ensure engine payload shape (generic)
    variables = _ensure_engine_keys(variables)

    # 4) Invoke engine
    print("[DOCSTRINGS] launching engine with keys:", sorted(list(variables.keys())))
    res = engine_run_v1(_TaskShim(variables), context={})

    # 5) Render artifacts and exit
    return _render_artifacts(res)


if __name__ == "__main__":
    sys.exit(main())

