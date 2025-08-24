from __future__ import annotations
from v2.backend.core.prompt_pipeline.executor.plugin_api import TaskAdapter, Prompt

try:
    from .prompt_builder import build_system_prompt, build_user_prompt
except Exception:
    def build_system_prompt(): return "You are a docstring rewriting assistant."
    def build_user_prompt(batch): return "..."

class DocstringTask(TaskAdapter):
    task_name = "docstrings"

    def prepare_items(self, rows):
        return list(rows)

    def build_prompt(self, batch):
        return Prompt(system=build_system_prompt(), user=build_user_prompt(batch))

    def parse_response(self, raw):
        return raw  # type: ignore

    def verify(self, item, result):
        return []

    def apply(self, item, result):
        return item.source or ""
