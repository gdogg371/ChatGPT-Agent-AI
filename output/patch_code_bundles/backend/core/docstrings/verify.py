from __future__ import annotations
from dataclasses import dataclass
import re
from typing import Tuple, List

@dataclass
class DocstringVerifier:
    def pep257_minimal(self, doc: str) -> Tuple[bool, List[str]]:
        issues: List[str] = []
        s = doc.strip()
        if not s: issues.append("empty docstring")
        else:
            first = s.splitlines()[0]
            if len(first) < 3: issues.append("summary line too short")
            if not first.endswith("."): issues.append("summary line should end with a period")
        return (len(issues) == 0, issues)

    def params_consistency(self, doc: str, signature: str) -> Tuple[bool, List[str]]:
        issues: List[str] = []
        m = re.search(r"\((.*?)\)", signature)
        params = []
        if m:
            for part in m.group(1).split(","):
                name = part.strip().split("=", 1)[0].strip()
                if name and name not in {"self", "cls"}: params.append(name)
        if "Args:" in doc:
            for p in params:
                if re.search(rf"\b{re.escape(p)}\b", doc) is None:
                    issues.append(f"param '{p}' not mentioned in docstring")
        return (len(issues) == 0, issues)
