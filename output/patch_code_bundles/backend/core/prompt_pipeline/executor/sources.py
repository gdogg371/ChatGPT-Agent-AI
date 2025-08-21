from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Dict, Iterator, Optional


@dataclass
class IntrospectionDbSource:
    url: str
    table: str = "introspection_index"
    status_filter: Optional[str] = "active"
    max_rows: Optional[int] = None

    def _connect(self) -> sqlite3.Connection:
        s = self.url.strip()
        if not s.startswith("sqlite:///"):
            raise ValueError(f"Only sqlite URLs supported in this prototype, got: {self.url}")
        path = s[len("sqlite:///") :]
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        return con

    def read_rows(self) -> Iterator[Dict]:
        sql = [f"SELECT id, filepath, symbol_type, name, lineno, description, unique_key_hash FROM {self.table}"]
        params = []
        if self.status_filter:
            sql.append("WHERE status = ?")
            params.append(self.status_filter)
        sql.append("ORDER BY id ASC")
        if isinstance(self.max_rows, int) and self.max_rows > 0:
            sql.append("LIMIT ?")
            params.append(self.max_rows)
        q = " ".join(sql)

        with self._connect() as con:
            for row in con.execute(q, params):
                yield {
                    "id": row["id"],
                    "filepath": row["filepath"],
                    "lineno": row["lineno"],
                    "name": row["name"],
                    "symbol_type": row["symbol_type"],
                    "description": row["description"],
                    "unique_key_hash": row["unique_key_hash"],
                }
