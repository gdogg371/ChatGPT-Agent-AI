"""Back-compat shim: keep old imports working.
Prefer: from patch_loop_test2.executor.engine import Engine
"""
from .engine import Engine as Orchestrator

# Optional: gentle deprecation warning (safe to remove later)
try:
    import warnings as _warnings
    _warnings.warn(
        "patch_loop_test3.executor.orchestrator is deprecated; use executor.engine.Engine",
        DeprecationWarning,
        stacklevel=2,
    )
except Exception:
    pass
