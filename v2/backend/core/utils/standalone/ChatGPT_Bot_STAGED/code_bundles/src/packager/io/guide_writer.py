# v2/backend/core/utils/code_bundles/code_bundles/src/packager/io/guide_writer.py
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
import json


class GuideWriter:
    """Robust GuideWriter that tolerates multiple call signatures and missing cfg fields."""

    def __init__(self, out_path: Path) -> None:
        self.out_path = Path(out_path)

    # ---------------------------- helpers ---------------------------------
    @staticmethod
    def _constraints(policy: Any) -> Dict[str, Any]:
        """
        Build a constraints dict without assuming specific policy types.
        Tries several common layouts; defaults to offline_only=True.
        """
        c: Dict[str, Any] = {}
        if policy is None:
            return {"offline_only": True}

        sandbox = getattr(policy, "sandbox", None)
        constraints = getattr(policy, "sandbox_constraints", None)
        exec_policy = getattr(policy, "execution_policy", None)

        def _get(holder: Any, name: str) -> Any:
            return getattr(holder, name, None) if holder is not None else None

        # offline_only
        offline_only = None
        offline_only = offline_only or _get(_get(sandbox, "constraints") or sandbox, "offline_only")
        offline_only = offline_only or _get(_get(exec_policy, "constraints") or exec_policy, "offline_only")
        offline_only = offline_only or _get(constraints, "offline_only")
        c["offline_only"] = bool(offline_only) if offline_only is not None else True

        # numeric/timeout-like limits (best-effort)
        def add_num(candidates: List[str], out_key: str) -> None:
            for nm in candidates:
                val = None
                for holder in (
                    _get(sandbox, "constraints") or sandbox,
                    exec_policy,
                    constraints,
                ):
                    if holder is None:
                        continue
                    val = getattr(holder, nm, None)
                    if val is not None:
                        c[out_key] = val
                        return

        add_num(["max_cpu_seconds", "cpu_seconds", "max_cpu"], "max_cpu_seconds")
        add_num(["timeout_seconds_per_run", "timeout_seconds", "timeout"], "timeout_seconds_per_run")
        return c

    @staticmethod
    def _limits(lim: Any) -> Dict[str, Any]:
        """Extract limits if present; otherwise return an empty dict."""
        if lim is None:
            return {}
        out: Dict[str, Any] = {}
        for key in [
            "max_files",
            "max_total_bytes",
            "max_record_bytes",
            "max_readme_bytes",
            "max_graph_bytes",
            "prompt_token_budget",
            "reply_token_budget",
        ]:
            v = getattr(lim, key, None)
            if v is not None:
                out[key] = v
        return out

    # ----------------------------- build ----------------------------------
    @classmethod
    def build(cls, *args, **kwargs) -> Dict[str, Any]:
        """
        Supported signatures (any of these):
          - build(reading_order, cfg, prompts_meta, split_info)
          - build(cfg=..., prompts_meta=..., split_info=..., reading_order=[...])
          - build(cfg=...)
        Returns a JSON-serializable guide dict.
        """
        # Extract from kwargs first
        reading_order = kwargs.pop("reading_order", None)
        cfg = kwargs.get("cfg") or kwargs.get("config")
        prompts_meta = kwargs.get("prompts_meta") or kwargs.get("prompts") or {}
        split_info = kwargs.get("split_info") or {}

        # If not provided via kwargs, attempt positional unpack
        if cfg is None and len(args) >= 2:
            reading_order = reading_order or (args[0] if isinstance(args[0], list) else None)
            cfg = args[1]
            prompts_meta = prompts_meta or (args[2] if len(args) > 2 else {})
            split_info = split_info or (args[3] if len(args) > 3 else {})

        # Defaults
        if reading_order is None:
            reading_order = [
                "Start at v2/patches/output/patch_code_bundles/ for the plain-text source.",
                "Then open analysis/contents_index.json for an inventory.",
                "Use analysis/roles.json and analysis/entrypoints.json to navigate.",
            ]

        policy = getattr(cfg, "policy", None) if cfg is not None else None
        limits = getattr(cfg, "limits", None) if cfg is not None else None

        out_bundle = getattr(cfg, "out_bundle", None)
        manifest_name = Path(out_bundle).name if out_bundle else "design_manifest.jsonl"

        guide: Dict[str, Any] = {
            "version": "1",
            "manifest": manifest_name,
            "reading_order": reading_order,
            "constraints": cls._constraints(policy),
            "limits": cls._limits(limits),
            "prompts_meta": prompts_meta or {},
            "split_info": split_info or {},
        }
        return guide

    # ----------------------------- write ----------------------------------
    def write(self, *args, **kwargs) -> None:
        """
        Compatibility writer.
        Accepts:
          - write(cfg, guide_obj, prompts_meta=None)
          - write(cfg, prompts_meta, split_info)  -> will call build()
        Persists to self.out_path as JSON.
        """
        # Normalize arguments
        cfg = None
        guide_obj: Optional[Dict[str, Any]] = None
        prompts_meta: Optional[Dict[str, Any]] = None
        split_info: Optional[Dict[str, Any]] = None

        if len(args) >= 1:
            cfg = args[0]
        if len(args) >= 2 and isinstance(args[1], dict) and "reading_order" in args[1]:
            guide_obj = args[1]
            if len(args) >= 3 and isinstance(args[2], dict):
                prompts_meta = args[2]
        else:
            # assume (cfg, prompts_meta, split_info) form
            if len(args) >= 2 and isinstance(args[1], dict):
                prompts_meta = args[1]
            if len(args) >= 3 and isinstance(args[2], dict):
                split_info = args[2]

        if guide_obj is None:
            guide_obj = self.build(cfg=cfg, prompts_meta=prompts_meta or {}, split_info=split_info or {})

        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self.out_path.write_text(
            json.dumps(guide_obj, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )

