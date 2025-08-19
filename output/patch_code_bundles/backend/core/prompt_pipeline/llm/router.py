from __future__ import annotations
from dataclasses import dataclass

@dataclass
class RoutingContext:
    task_type: str = "docstring_rewrite"
    avg_input_tokens: int = 0
    needs_vision: bool = False
    needs_tools: bool = False
    cost_tier: str = "low"

class ModelRouter:
    def __init__(self, default: str = "auto"):
        self.default = default

    def choose(self, ctx: RoutingContext, override: str | None = None) -> str:
        if override and override != "auto": return override
        if ctx.needs_vision: return "gpt-4o-mini"
        if ctx.task_type == "docstring_rewrite" and ctx.avg_input_tokens < 12000: return "gpt-4o-mini"
        return "gpt-4o-mini"
