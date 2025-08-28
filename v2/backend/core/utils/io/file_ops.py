from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path

@dataclass(slots=True)
class FileOpsConfig:
    normalize_newlines: bool = True
    preserve_crlf: bool = False  # when True, write CRLF instead of LF

class FileOps:
    def __init__(self, cfg: FileOpsConfig | None = None):
        self.cfg = cfg or FileOpsConfig()

    def read_text(self, p: Path) -> str:
        txt = p.read_text(encoding="utf-8", errors="ignore")
        return self._norm(txt)

    def write_text(self, p: Path, text: str, *, preserve_crlf: bool | None = None) -> None:
        """Write text, optionally overriding CRLF preservation per call."""
        p.parent.mkdir(parents=True, exist_ok=True)
        s = self._norm(text)
        if preserve_crlf is True:
            s = s.replace("\n", "\r\n")
        elif preserve_crlf is False:
            # force LF
            s = s.replace("\r\n", "\n").replace("\r", "\n")
        else:
            # use config default
            if self.cfg.preserve_crlf:
                s = s.replace("\n", "\r\n")
        p.write_text(s, encoding="utf-8")

    def _norm(self, s: str) -> str:
        if not self.cfg.normalize_newlines:
            return s
        return s.replace("\r\n", "\n").replace("\r", "\n")
