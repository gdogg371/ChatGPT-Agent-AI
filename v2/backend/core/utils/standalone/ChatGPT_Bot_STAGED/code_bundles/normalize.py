# normalize.py
from __future__ import annotations

from dataclasses import dataclass
from typing import List
import hashlib

from bundle_io import FileRec


@dataclass(frozen=True)
class NormalizationRules:
    """
    Normalization rules applied to text-like files.

    newline_policy: 'lf' or 'crlf'
    encoding:       text encoding used to decode/encode bytes (best-effort)
    strip_trailing_ws:
        - If True: strip trailing whitespace on each line and ensure a
          single trailing newline at EOF (if the file is non-empty).
    excluded_paths:
        - Tuple of case-insensitive path *prefixes* to skip normalization
          for (evaluation happens against the emitted, posix-style path).
    """
    newline_policy: str = "lf"
    encoding: str = "utf-8"
    strip_trailing_ws: bool = True
    excluded_paths: tuple[str, ...] = ()


class Normalizer:
    """
    Class-based normalizer that mirrors the original module’s behavior.

    Usage:
        n = Normalizer(rules)
        out_files = n.apply(files)
    """

    def __init__(self, rules: NormalizationRules) -> None:
        self.rules = rules

    # ---------- public API ----------

    def apply(self, files: List[FileRec]) -> List[FileRec]:
        out: List[FileRec] = []
        for fr in files:
            if self._should_skip(fr.path):
                out.append(fr)
                continue
            try:
                txt = self._to_text(fr.data)
            except Exception:
                # Non-decodable -> leave as-is
                out.append(fr)
                continue

            if self.rules.strip_trailing_ws:
                # Strip trailing whitespace per line and ensure single trailing \n
                txt = "\n".join(line.rstrip() for line in txt.splitlines())
                if txt and not txt.endswith("\n"):
                    txt += "\n"

            blob = self._from_text(txt)
            out.append(FileRec(path=fr.path, data=blob, sha256=hashlib.sha256(blob).hexdigest()))
        return out

    # ---------- internals ----------

    def _should_skip(self, path: str) -> bool:
        path_lower = path.lower()
        return any(path_lower.startswith(prefix.lower()) for prefix in self.rules.excluded_paths)

    def _to_text(self, data: bytes) -> str:
        return data.decode(self.rules.encoding, errors="replace")

    def _from_text(self, text: str) -> bytes:
        if self.rules.newline_policy == "lf":
            text = text.replace("\r\n", "\n").replace("\r", "\n")
        elif self.rules.newline_policy == "crlf":
            text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
        # Any other value: leave newlines as-is
        return text.encode(self.rules.encoding)


# -------- legacy shim (keeps existing callers working) --------

def apply_normalization(files: List[FileRec], rules: NormalizationRules) -> List[FileRec]:
    """Backward-compatible function alias."""
    return Normalizer(rules).apply(files)
