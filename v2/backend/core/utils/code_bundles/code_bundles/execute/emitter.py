# v2/backend/core/utils/code_bundles/code_bundles/execute/emitter.py

from __future__ import annotations
import sys
from pathlib import Path

# Ensure the embedded packager is importable first
ROOT = Path(__file__).parent
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# ---- analysis emitter wiring (robust loader) ----
import importlib.util
from importlib.machinery import SourceFileLoader

__all__ = ["_load_analysis_emitter"]


def _load_analysis_emitter(project_root: Path):
    """
    Robust loader for optional analysis_emitter.py co-located in src/packager.
    """
    here = Path(__file__).resolve().parent
    cand = here / "src" / "packager" / "analysis_emitter.py"
    if not cand.exists():
        return None
    spec = importlib.util.spec_from_loader("analysis_emitter", SourceFileLoader("analysis_emitter", str(cand)))
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod
