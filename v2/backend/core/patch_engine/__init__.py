# File: backend/core/patch_engine/__init__.py
"""
Lightweight, file-only patch engine primitives (no DB, no CLI).
Designed to run interactively (e.g., from PyCharm).

Main components:
- PatchEngineConfig: configuration + safety limits
- Scope: inscope resolver & patch path validation
- WorkspaceManager: mirror/working-copy lifecycle, snapshots & promotion
- Evaluator: initial/exhaustive test runners (optional commands)
- PatchApplier: unified diff application via 'git apply'
- RunManifest: per-run manifest writer/reader
- Interactive runner: run_one(...) helper

Tip: Open and run 'backend/core/patch_engine/interactive_run.py' in PyCharm.
"""

from .config import PatchEngineConfig, SafetyLimits
from .scope import Scope
from .workspace import WorkspaceManager
from .evaluator import Evaluator, TestPhase, TestResult
from .applier import PatchApplier, ApplyResult
from .run_manifest import RunManifest, new_run_id

__all__ = [
    "PatchEngineConfig",
    "SafetyLimits",
    "Scope",
    "WorkspaceManager",
    "Evaluator",
    "TestPhase",
    "TestResult",
    "PatchApplier",
    "ApplyResult",
    "RunManifest",
    "new_run_id",
]
