# File: v2/backend/core/introspect/read_docstrings.py
from __future__ import annotations
r"""
Docstring reader/summarizer that walks a Python tree, summarizes docstrings via
llama.cpp, and writes rows using DocstringWriter for the introspection index.

- Prefers your model "mistral-7b-instruct-v0.1.Q4_K_M.gguf" automatically if present.
- Concise, relevant console prints: model load start/end, writer init, traversal start,
  per-file progress lines, and a final summary.
- Defaults scan root to the detected repository root (no env needed).
- Windows-friendly: default use_mlock=False to avoid stalls; can be overridden via env.
"""

import os
import ast
import time
import tempfile
import hashlib
from pathlib import Path
from typing import List

from llama_cpp import Llama  # type: ignore

from v2.backend.core.db.writers.docstring_writer import DocstringWriter
from v2.backend.core.db.access.db_init import DB_PATH


# ---- Repo / model discovery --------------------------------------------------


def _guess_repo_root() -> Path:
    """Walk up until we find a folder that looks like the repo (has 'software/ai_models' or '.git')."""
    p = Path(__file__).resolve().parent
    for _ in range(8):
        if (p / "software" / "ai_models").exists() or (p / ".git").exists():
            return p
        p = p.parent
    # Fallback: a few levels up from this file
    try:
        return Path(__file__).resolve().parents[4]
    except Exception:
        return Path.cwd().resolve()


REPO_ROOT = _guess_repo_root()

# Default models dir under the repo; no env vars required.
DOC_LLM_DIR = Path(os.getenv("DOCSTRING_MODELS_DIR", str(REPO_ROOT / "software" / "ai_models" / "mistral")))
DOC_LLM_GGUF = os.getenv("DOCSTRING_LLAMA_GGUF")  # optional explicit override

# Tunables (env can override if desired)
def _as_bool(x: str | None, default: bool) -> bool:
    if x is None:
        return default
    s = x.strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return default


DOC_LLM_CTX = int(os.getenv("DOCSTRING_LLAMA_CTX", "2048"))
DOC_LLM_THREADS = int(os.getenv("DOCSTRING_LLAMA_THREADS", "8"))
DOC_LLM_GPU_LAYERS = int(os.getenv("DOCSTRING_LLAMA_GPU_LAYERS", "0"))
DOC_LLM_MLOCK = _as_bool(os.getenv("DOCSTRING_LLAMA_MLOCK"), default=False if os.name == "nt" else True)

# Preferred exact filename if present in the dir (your request)
PREFERRED_GGUF_NAME = "mistral-7b-instruct-v0.1.Q4_K_M.gguf"


def _find_gguf(p: Path) -> Path | None:
    """
    Pick a .gguf: if p is a file, return it; if dir:
      1) prefer PREFERRED_GGUF_NAME if present
      2) otherwise pick largest *.gguf
    """
    p = Path(p)
    if p.is_file() and p.suffix.lower() == ".gguf":
        return p
    if p.is_dir():
        pref = p / PREFERRED_GGUF_NAME
        if pref.exists():
            return pref
        cands = sorted(p.glob("*.gguf"), key=lambda q: q.stat().st_size, reverse=True)
        return cands[0] if cands else None
    return None


# ---- Output controls ---------------------------------------------------------


def _stderr_to_oneliner(text: str, max_len: int = 220) -> str:
    """Collapse whitespace/newlines and truncate."""
    clean = " ".join((text or "").strip().split())
    return clean[:max_len] + ("…" if len(clean) > max_len else "")


def _print_stderr_summary(stage: str, stderr_bytes: bytes) -> None:
    """Print a single formatted line for captured stderr, if any."""
    if not stderr_bytes:
        return
    try:
        txt = stderr_bytes.decode("utf-8", errors="ignore")
    except Exception:
        txt = repr(stderr_bytes)
    oneliner = _stderr_to_oneliner(txt)
    if oneliner:
        print(f"[LLM ⚠️] {stage}: {oneliner}")


class _SuppressStdoutCaptureStderr:
    """
    Redirect **C-level** stdout (fd=1) to /dev/null (fully suppressed),
    and capture **C-level** stderr (fd=2) to a temp file.
    Our Python-level prints are unaffected.
    """

    def __init__(self) -> None:
        self._devnull_fd = None
        self._old_stdout_fd = None
        self._old_stderr_fd = None
        self._stderr_tmp = None

    def __enter__(self):
        self._devnull_fd = os.open(os.devnull, os.O_WRONLY)
        self._old_stdout_fd = os.dup(1)
        self._old_stderr_fd = os.dup(2)
        os.dup2(self._devnull_fd, 1)  # stdout -> null
        self._stderr_tmp = tempfile.TemporaryFile(mode="w+b")
        os.dup2(self._stderr_tmp.fileno(), 2)  # stderr -> temp
        return self

    def read(self) -> bytes:
        if not self._stderr_tmp:
            return b""
        try:
            self._stderr_tmp.flush()
            self._stderr_tmp.seek(0)
            return self._stderr_tmp.read()
        except Exception:
            return b""

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._old_stdout_fd is not None:
                os.dup2(self._old_stdout_fd, 1)
            if self._old_stderr_fd is not None:
                os.dup2(self._old_stderr_fd, 2)
        finally:
            for h in (self._stderr_tmp,):
                try:
                    h and h.close()
                except Exception:
                    pass
            for fd in (self._devnull_fd, self._old_stdout_fd, self._old_stderr_fd):
                try:
                    fd is not None and os.close(fd)
                except Exception:
                    pass


class _CaptureOnlyStderr:
    """Capture **C-level** stderr (fd=2) to a temp file while leaving stdout alone."""

    def __init__(self) -> None:
        self._old_stderr_fd = None
        self._stderr_tmp = None

    def __enter__(self):
        self._old_stderr_fd = os.dup(2)
        self._stderr_tmp = tempfile.TemporaryFile(mode="w+b")
        os.dup2(self._stderr_tmp.fileno(), 2)
        return self

    def read(self) -> bytes:
        if not self._stderr_tmp:
            return b""
        try:
            self._stderr_tmp.flush()
            self._stderr_tmp.seek(0)
            return self._stderr_tmp.read()
        except Exception:
            return b""

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._old_stderr_fd is not None:
                os.dup2(self._old_stderr_fd, 2)
        finally:
            for h in (self._stderr_tmp,):
                try:
                    h and h.close()
                except Exception:
                    pass
            try:
                self._old_stderr_fd is not None and os.close(self._old_stderr_fd)
            except Exception:
                pass


# ---- Analyzer ----------------------------------------------------------------


class DocStringAnalyzer:
    # Defaults (no env required)
    ROOT_DIR = str(REPO_ROOT)  # will be resolved in __init__
    MODEL_PATH: str = ""

    EXCLUDED_DIRS = {"__pycache__", "venv", "env", ".git", "tests", "tests_adhoc", "site-packages"}
    CHAR_COUNT_THRESHOLD = 50

    WRITER_MODE = "introspection_index"
    AGENT_ID = 1

    def __init__(self) -> None:
        # Quiet llama at the source (no log callback; avoids ctypes/unraisablehook warnings)
        os.environ.setdefault("LLAMA_LOG_LEVEL", "50")  # 50=CRITICAL

        # Resolve model path
        mp = Path(DOC_LLM_GGUF) if DOC_LLM_GGUF else _find_gguf(DOC_LLM_DIR)
        if not mp or not mp.exists():
            raise FileNotFoundError(
                f"Docstring model not found. "
                f"Searched DOCSTRING_LLAMA_GGUF={DOC_LLM_GGUF!r} "
                f"and directory {DOC_LLM_DIR}"
            )
        self.MODEL_PATH = str(mp)

        size_mb = (mp.stat().st_size or 0) / (1024 * 1024)
        print(
            f"[DocStringAnalyzer] Loading model {mp.name} ({size_mb:.1f} MB) "
            f"| ctx={DOC_LLM_CTX} | threads={DOC_LLM_THREADS} "
            f"| mlock={'on' if DOC_LLM_MLOCK else 'off'} | gpu_layers={DOC_LLM_GPU_LAYERS}"
        )

        t0 = time.time()
        with _SuppressStdoutCaptureStderr() as cap:
            self.llm = Llama(
                model_path=self.MODEL_PATH,
                n_ctx=DOC_LLM_CTX,
                n_threads=DOC_LLM_THREADS,
                n_gpu_layers=DOC_LLM_GPU_LAYERS,
                use_mlock=DOC_LLM_MLOCK,
                verbose=False,
            )
        _print_stderr_summary("load", cap.read())
        print(f"[DocStringAnalyzer] Model loaded in {time.time() - t0:.2f}s")

        # Scan root (no env required; use repo root by default)
        chosen_root = os.getenv("DOCSTRING_ROOT") or self.ROOT_DIR
        self.root_path = Path(chosen_root).resolve()
        if not self.root_path.is_dir():
            raise FileNotFoundError(f"Docstring scan root not found: {self.root_path}")

        # DB env & writer (minimal, relevant prints around it)
        os.environ.setdefault("SQLITE_DB_URL", f"sqlite:///{DB_PATH}")
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

        print("[DocStringAnalyzer] Initializing writer…")
        self.writer = DocstringWriter(agent_id=self.AGENT_ID, mode=self.WRITER_MODE)
        print("[DocStringAnalyzer] Writer ready.")

        self.seen_docstrings: set[str] = set()

    # ----- AST helpers -----

    def annotate_parents(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                child.parent = node  # type: ignore[attr-defined]

    def get_enclosing_class(self, func_node: ast.AST) -> str | None:
        parent = getattr(func_node, "parent", None)
        while parent:
            if isinstance(parent, ast.ClassDef):
                return parent.name
            parent = getattr(parent, "parent", None)
        return None

    # ----- LLM summarization -----

    def summarize_docstring(self, docstring: str | None) -> str:
        text = (docstring or "").strip()
        if len(text) <= self.CHAR_COUNT_THRESHOLD:
            return "Bad docstring"
        if text in self.seen_docstrings:
            return "Duplicate docstring — skipped"
        self.seen_docstrings.add(text)

        prompt = (
            "Summarize this Python docstring as a concise one-liner:\n"
            f"\"\"\"{text}\"\"\"\n"
            "Summary:"
        )
        try:
            with _CaptureOnlyStderr() as cap:
                response = self.llm(prompt, max_tokens=64, stop=["\n"])
            _print_stderr_summary("infer", cap.read())
            summary_text = (response.get("choices", [{}])[0].get("text") or "").strip()  # type: ignore[dict-item]
            return summary_text if summary_text else "Bad docstring"
        except Exception as e:
            print(f"[DocStringAnalyzer ❌] LLM summarization failed: {e}")
            return "LLM failed"

    # ----- Extraction -----

    def extract_docstrings(self, filepath: str) -> list[dict]:
        """
        Returns a list of entry dicts with minimal, model-agnostic keys.
        We'll adapt them to DocstringWriter's schema later.
        """
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                source = f.read()
            tree = ast.parse(source, filename=filepath)
            self.annotate_parents(tree)
        except Exception as e:
            print(f"[DocStringAnalyzer ❌] Failed to parse {filepath}: {e}")
            return []

        rel_path = os.path.relpath(filepath, self.root_path).replace("\\", "/")
        subdir = os.path.dirname(rel_path).replace("\\", "/")
        filename = os.path.basename(filepath)
        language = "Python" if filename.endswith(".py") else "Unknown"

        entries: list[dict] = []

        # Module-level docstring
        mod_doc = ast.get_docstring(tree)
        if mod_doc:
            summary = self.summarize_docstring(mod_doc)
            entries.append(
                {
                    "subdir": subdir,
                    "file_basename": filename,
                    "line": 1,
                    "language": language,
                    "class": "-",
                    "function": "-",
                    "summary": summary,
                    "_rel_path": rel_path,
                }
            )

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                doc = ast.get_docstring(node)
                summary = self.summarize_docstring(doc) if doc else "Bad docstring"
                entries.append(
                    {
                        "subdir": subdir,
                        "file_basename": filename,
                        "line": node.lineno,
                        "language": language,
                        "class": node.name,
                        "function": "-",
                        "summary": summary,
                        "_rel_path": rel_path,
                    }
                )
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node)
                summary = self.summarize_docstring(doc) if doc else "Bad docstring"
                class_name = self.get_enclosing_class(node)
                entries.append(
                    {
                        "subdir": subdir,
                        "file_basename": filename,
                        "line": node.lineno,
                        "language": language,
                        "class": class_name or "-",
                        "function": node.name,
                        "summary": summary,
                        "_rel_path": rel_path,
                    }
                )

        return entries

    # ----- DocstringWriter adapter -----

    @staticmethod
    def _hash_for_writer(filepath: str, symbol_type: str, symbol_name: str, lineno: int) -> str:
        import hashlib as _h
        h = _h.sha1(f"{filepath}|{symbol_type}|{symbol_name}|{lineno}".encode("utf-8")).hexdigest()
        return f"hash_{symbol_name}_{h[:10]}"

    @staticmethod
    def _to_writer_row(entry: dict) -> dict:
        """
        Adapt our extractor entry -> DocstringWriter's expected row shape for mode='introspection_index'.
        """
        rel_path = (entry.get("_rel_path") or "").strip().replace("\\", "/")
        if not rel_path:
            raise ValueError("Missing _rel_path in entry")
        line = int(entry.get("line", 1))
        if line < 1:
            line = 1

        cls = entry.get("class") or "-"
        func = entry.get("function") or "-"

        if func != "-":
            symbol_type = "function"
            symbol_name = func
            writer_name_key = "function"
        elif cls != "-":
            symbol_type = "class"
            symbol_name = cls
            writer_name_key = "name"
        else:
            symbol_type = "module"
            symbol_name = Path(entry.get("file_basename", "")).stem or "unknown"
            writer_name_key = "name"

        description = (entry.get("summary") or "").strip()
        unique_hash = DocStringAnalyzer._hash_for_writer(rel_path, symbol_type, symbol_name, line)

        row = {
            "file": rel_path,
            "filetype": symbol_type,
            "line": line,
            "route_method": None,
            "route_path": None,
            "ag_tag": "AG-Introspection",
            "description": description,
            "target": None,
            "relation": None,
            "hash": unique_hash,
            "status": "active",
            "function": None,
            "route": None,
            "name": None,
            "subdir": entry.get("subdir", ""),
            "analyzer": "DocStringAnalyzer",
        }
        row[writer_name_key] = symbol_name
        return row

    # ----- Traverse & write -----

    def traverse_and_write(self) -> dict:
        total_files = 0
        total_written = 0
        total_skipped = 0
        total_failed = 0
        total_llm = 0

        print(f"[DocStringAnalyzer] Starting traversal at {self.root_path}")
        for dirpath, dirnames, filenames in os.walk(self.root_path):
            dirnames[:] = [d for d in dirnames if d not in self.EXCLUDED_DIRS]
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                total_files += 1
                full_path = Path(dirpath) / filename
                rel_path = full_path.relative_to(self.root_path).as_posix()
                print(f"[DocStringAnalyzer] Processing {rel_path}")
                try:
                    records = self.extract_docstrings(str(full_path))
                    for entry in records:
                        summary = (entry.get("summary") or "").strip()
                        if "LLM failed" in summary:
                            total_failed += 1
                            outcome = "LLM summarization failed"
                        elif "Duplicate" in summary:
                            total_skipped += 1
                            continue
                        elif "Bad docstring" in summary:
                            outcome = "Too short or missing docstring"
                        else:
                            total_llm += 1
                            outcome = "Docstring summarized successfully"

                        writer_row = self._to_writer_row(entry)
                        self.writer.write(writer_row)
                        total_written += 1

                        symbol_type = writer_row["filetype"]
                        symbol_name = writer_row.get("function") or writer_row.get("route") or writer_row.get("name")
                        desc_snip = " ".join((writer_row["description"] or "").split())[:120]
                        if len((writer_row["description"] or "")) > 120:
                            desc_snip += "…"
                        print(
                            f"[DocStringAnalyzer ✅] {writer_row['file']}:{writer_row['line']} "
                            f"{symbol_type}={symbol_name} — {outcome} — {desc_snip}"
                        )
                except Exception as e:
                    print(f"[DocStringAnalyzer ❌] Error during processing {rel_path}: {e}")
                    total_failed += 1

        return dict(
            total_files=total_files,
            total_written=total_written,
            total_skipped=total_skipped,
            total_failed=total_failed,
            total_llm=total_llm,
        )


# ---- Main --------------------------------------------------------------------

if __name__ == "__main__":
    analyzer = DocStringAnalyzer()
    print(f"\n[DocStringAnalyzer] Root: {analyzer.root_path}")
    print(f"[DocStringAnalyzer] Model: {analyzer.MODEL_PATH}")
    print(f"[DocStringAnalyzer] Writer mode: {analyzer.WRITER_MODE}")

    start = time.time()
    stats = analyzer.traverse_and_write()
    duration = round(time.time() - start, 2)

    print(f"\n[DocStringAnalyzer ✅] Completed in {duration} sec")
    print(f"[DocStringAnalyzer] Files scanned: {stats['total_files']}")
    print(f"[DocStringAnalyzer] Records written: {stats['total_written']}")
    print(f"[DocStringAnalyzer] Duplicate skipped: {stats['total_skipped']}")
    print(f"[DocStringAnalyzer ❌] Parse/LLM failures: {stats['total_failed']}")
    print(f"[DocStringAnalyzer] LLM summaries created: {stats['total_llm']}")

