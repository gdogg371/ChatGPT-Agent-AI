from __future__ import annotations

def _one_line(s: str, max_len: int = 160) -> str:
    s = " ".join((s or "").split())
    return s[:max_len] + ("…" if len(s) > max_len else "")

class ConsoleLog:
    def __init__(self, tag: str):
        self.tag = tag

    def info(self, msg: str):
        print(f"[{self.tag} ✅] {_one_line(msg)}")

    def warn(self, msg: str):
        print(f"[{self.tag} ⚠️] {_one_line(msg)}")

    def error(self, msg: str):
        print(f"[{self.tag} ❌] {_one_line(msg)}")

    def stage(self, emoji: str, msg: str):
        print(f"[{self.tag} {emoji}] {_one_line(msg)}")
