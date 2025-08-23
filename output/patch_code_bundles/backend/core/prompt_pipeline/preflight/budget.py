from __future__ import annotations
from dataclasses import dataclass
from typing import List, Dict, Callable

@dataclass(slots=True)
class TokenBudgeter:
    max_ctx: int
    resp_per_item: int
    guardrail: int = 1024
    batch_overhead_tokens: int = 800  # account for system + schema + delimiters

    def estimate(self, s: str) -> int:
        # 4 chars/token heuristic + 10% headroom
        return int(len(s) / 4 * 1.1) + 1

    def pack(self, items: List[Dict], serialize_fn: Callable[[List[Dict]], str]) -> List[List[Dict]]:
        """Greedy pack with batch overhead; single-item fallback if too large."""
        batches: List[List[Dict]] = []
        cur: List[Dict] = []
        cur_tokens = 0
        max_input = self.max_ctx - self.guardrail - self.resp_per_item - self.batch_overhead_tokens

        for it in items:
            t = self.estimate(serialize_fn([it]))
            if t > max_input:
                # force single-item batch rather than overflow
                batches.append([it])
                continue
            next_resp = self.resp_per_item * (len(cur) + 1)
            avail = self.max_ctx - self.guardrail - next_resp - self.batch_overhead_tokens
            if cur and (cur_tokens + t) > avail:
                batches.append(cur); cur = []; cur_tokens = 0
            cur.append(it); cur_tokens += t

        if cur:
            batches.append(cur)
        return batches
