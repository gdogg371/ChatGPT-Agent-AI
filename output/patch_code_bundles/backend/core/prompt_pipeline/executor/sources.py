# File: v2/backend/core/prompt_pipeline/executor/sources.py
from __future__ import annotations

"""
Spine-forwarding Introspection source.

Replaces direct DB access with a Spine capability call so that the executor
does not depend on DB code. Mirrors the old API and yielded row shape.

Yields dicts with keys:
  id, filepath, lineno, name, symbol_type, description, unique_key_hash
"""

from dataclasses import dataclass
from typing import Any, Dict, Iterator, Iterable, Optional

from v2.backend.core.spine import Spine
from v2.backend.core.configuration.spine_paths import SPINE_CAPS_PATH


@dataclass
class IntrospectionDbSource:
    url: str
    table: str = "introspection_index"
    status_filter: Optional[str] = "active"
    max_rows: Optional[int] = None

    def _fetch_rows_via_spine(self) -> Iterable[Dict[str, Any]]:
        spine = Spine(caps_path=SPINE_CAPS_PATH)
        payload: Dict[str, Any] = {"table": self.table, "order_by": "id ASC"}

        # Preserve legacy filters/limits
        if self.status_filter:
            payload["where"] = {"status": self.status_filter}
        if isinstance(self.max_rows, int) and self.max_rows > 0:
            payload["limit"] = int(self.max_rows)

        arts = spine.dispatch_capability(
            capability="introspection.fetch.v1",
            payload=payload,
            intent="discover",
            subject=self.table,
            context={"executor": "prompt_pipeline.executor.sources"},
        )

        # If provider returned a Problem, surface a crisp error
        for a in arts:
            if a.kind == "Problem":
                prob = (a.meta or {}).get("problem", {})
                code = prob.get("code", "ProviderError")
                msg = prob.get("message", "unknown error")
                raise RuntimeError(f"introspection.fetch.v1 failed: {code}: {msg}")

        rows: Iterable[Dict[str, Any]] = []
        if len(arts) == 1 and isinstance(arts[0].meta, dict) and "result" in arts[0].meta:
            res = arts[0].meta["result"]
            if isinstance(res, list):
                rows = [r for r in res if isinstance(r, dict)]
            elif isinstance(res, dict) and isinstance(res.get("records"), list):
                rows = [r for r in res["records"] if isinstance(r, dict)]
        return rows

    def read_rows(self) -> Iterator[Dict]:
        """
        Yield rows shaped like the legacy sqlite prototype:

            {
              "id": int|str,
              "filepath": "path/relative.py",
              "lineno": int,
              "name": str,
              "symbol_type": str,
              "description": str,
              "unique_key_hash": str
            }
        """
        for row in self._fetch_rows_via_spine():
            # Map flexible column names to the expected shape
            filepath = (
                row.get("filepath")
                or row.get("file")
                or row.get("relpath")
                or row.get("path")
                or ""
            )
            lineno = row.get("lineno", row.get("line", 0))
            try:
                lineno = int(lineno or 0)
            except Exception:
                lineno = 0

            yield {
                "id": row.get("id"),
                "filepath": filepath,
                "lineno": lineno,
                "name": row.get("name") or "",
                "symbol_type": row.get("symbol_type") or row.get("type") or "",
                "description": row.get("description") or row.get("summary") or "",
                "unique_key_hash": row.get("unique_key_hash") or row.get("hash") or "",
            }

