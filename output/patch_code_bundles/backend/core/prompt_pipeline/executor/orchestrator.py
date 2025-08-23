# File: v2/backend/core/prompt_pipeline/executor/orchestrator.py
from __future__ import annotations

"""
Back-compat shim for legacy imports.

Older code may import `executor.orchestrator` and expect an `Orchestrator`
(or even an `Engine`) object with a `.run()` method that kicks off the
LLM patch loop. This shim preserves that surface but forwards the work
to the Spine capability `llm.engine.run.v1`.

Prefer calling the Spine directly or using the engine provider:
    from v2.backend.core.prompt_pipeline.executor.engine import run_v1
"""

from dataclasses import dataclass
from typing import Any, Dict, List

from v2.backend.core.spine import Spine
from v2.backend.core.spine.contracts import Artifact
from v2.backend.core.configuration.spine_paths import SPINE_CAPS_PATH


@dataclass
class Orchestrator:
    """Compatibility wrapper that calls the spine engine capability."""
    payload: Dict[str, Any]

    def run(self) -> List[Artifact]:
        spine = Spine(caps_path=SPINE_CAPS_PATH)
        return spine.dispatch_capability(
            capability="llm.engine.run.v1",
            payload=self.payload,
            intent="pipeline",
            subject=self.payload.get("project_root") or "-",
            context={"shim": "executor.orchestrator"},
        )


# Backwards-compat alias: some callers used `Engine` previously.
Engine = Orchestrator


