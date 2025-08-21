# File: backend/core/introspect/read_docstrings.py
from __future__ import annotations

"""
Docstring reader/summarizer that walks a Python tree, summarizes docstrings via
llama.cpp, and writes rows using DocstringWriter for the introspection index.

Fixes in this version:
- Correctly labels module-level functions as `function` (no longer mis-tagged as `module`).
- Keeps class methods as `function`, classes as `class`, and files without symbols as `module`.
- Safer, explicit default scan root changed from filesystem root to:
    - Windows:  r"\\ChatGPT Bot"
    - POSIX:    "/ChatGPT Bot"
  (Override with DOCSTRING_ROOT environment variable if desired.)
- Cosmetic: entry field previously named "filetype" (which looked like language)
  is renamed to "language" to avoid confusion with the writer's `symbol_type`.

Additional update:
- Auto-discover a local GGUF under <repo>/software/ai_models/mistral (or use DOCSTRING_LLAMA_GGUF),
  so Windows paths like 'C:\\Users\\...\\ChatGPT Bot\\software\\ai_models\\mistral' work out of the box.
"""

import os
import ast
import time
import tempfile
import hashlib
from pathlib import Path

from llama_cpp import Llama  # type: ignore

# llama_log_set may not exist on older versions; we guard usage below
try:
    from llama_cpp import llama_log_set  # type: ignore
except Exception:  # pragma: no cover - optional API
    llama_log_set = None  # type: ignore

from v2.backend.core.db.writers.docstring_writer import DocstringWriter
from v2.backend.core.db.access.db_init import DB_PATH



# ---- Repo / model discovery --------------------------------------------------

def _guess_repo_root() -> Path:
    """Walk up a few levels until we find a plausible repo root."""
    p = Path(__file__).resolve().parent
    for _ in range(8):
        if (p / "software" / "ai_models").exists() or (p / ".git").exists():
            return p
        p = p.parent
    # Fallback: 5 levels up (…/v2/backend/core/introspect/ -> repo)
    try:
        return Path(__file__).resolve().parents[4]
    except Exception:
        return Path.cwd().resolve()


REPO_ROOT = _guess_repo_root()

# Default models dir: <repo>/software/ai_models/mistral (can override via env)
DOC_LLM_DIR = Path(os.getenv("DOCSTRING_MODELS_DIR", str(REPO_ROOT / "software" / "ai_models" / "mistral")))
# Optional explicit file override
DOC_LLM_GGUF = os.getenv("DOCSTRING_LLAMA_GGUF")
DOC_LLM_CTX = int(os.getenv("DOCSTRING_LLAMA_CTX", "2048"))


def _find_gguf(p: Path) -> Path | None:
    """
    Pick a .gguf: if p is a file, return it; if dir, pick largest *.gguf; else None.
    """
    p = Path(p)
    if p.is_file() and p.suffix.lower() == ".gguf":
        return p
    if p.is_dir():
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


def _one_line(s: str, max_len: int = 140) -> str:
    """Single-line, truncated snippet for console."""
    s = " ".join((s or "").split())
    return s[:max_len] + ("…" if len(s) > max_len else "")


class _SuppressStdoutCaptureStderr:
    """
    Redirect **C-level** stdout (fd=1) to /dev/null (fully suppressed),
    and capture **C-level** stderr (fd=2) to a temp file.

    Python-level prints are unaffected (our own prints still show).
    Use as context manager.
    """

    def __init__(self) -> None:
        self._devnull_fd = None
        self._old_stdout_fd = None
        self._old_stderr_fd = None
        self._stderr_tmp = None

    def __enter__(self):
        # Open /dev/null for stdout suppression
        self._devnull_fd = os.open(os.devnull, os.O_WRONLY)

        # Duplicate original fds so we can restore later
        self._old_stdout_fd = os.dup(1)
        self._old_stderr_fd = os.dup(2)

        # Redirect stdout -> /dev/null
        os.dup2(self._devnull_fd, 1)

        # Redirect stderr -> temp file (binary)
        self._stderr_tmp = tempfile.TemporaryFile(mode="w+b")
        os.dup2(self._stderr_tmp.fileno(), 2)
        return self

    def read(self) -> bytes:
        """Read captured stderr bytes."""
        if not self._stderr_tmp:
            return b""
        try:
            self._stderr_tmp.flush()
            self._stderr_tmp.seek(0)
            return self._stderr_tmp.read()
        except Exception:
            return b""

    def __exit__(self, exc_type, exc, tb):
        # Restore stdout/stderr
        try:
            if self._old_stdout_fd is not None:
                os.dup2(self._old_stdout_fd, 1)
            if self._old_stderr_fd is not None:
                os.dup2(self._old_stderr_fd, 2)
        finally:
            # Close temp/devnull and old dup fds
            if self._stderr_tmp is not None:
                try:
                    self._stderr_tmp.close()
                except Exception:
                    pass
            if self._devnull_fd is not None:
                try:
                    os.close(self._devnull_fd)
                except Exception:
                    pass
            if self._old_stdout_fd is not None:
                try:
                    os.close(self._old_stdout_fd)
                except Exception:
                    pass
            if self._old_stderr_fd is not None:
                try:
                    os.close(self._old_stderr_fd)
                except Exception:
                    pass


class _CaptureOnlyStderr:
    """
    Capture **C-level** stderr (fd=2) to a temp file while leaving stdout alone.
    Use this during inference to keep token stream intact but still summarize errors.
    """

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
            if self._stderr_tmp is not None:
                try:
                    self._stderr_tmp.close()
                except Exception:
                    pass
            if self._old_stderr_fd is not None:
                try:
                    os.close(self._old_stderr_fd)
                except Exception:
                    pass


# ---- Analyzer ----------------------------------------------------------------

class DocStringAnalyzer:
    # Overridable via env if needed
    _DEFAULT_ROOT = r"\ChatGPT Bot" if os.name == "nt" else "/ChatGPT Bot"

    ROOT_DIR = os.environ.get(
        "DOCSTRING_ROOT",
        _DEFAULT_ROOT,
    )

    # NOTE: MODEL_PATH is set at runtime after discovery so prints show the actual file.
    MODEL_PATH: str = ""

    EXCLUDED_DIRS = {"__pycache__", "venv", "env", ".git", "tests", "tests_adhoc", "site-packages"}
    CHAR_COUNT_THRESHOLD = 50
    WRITER_MODE = "introspection_index"
    AGENT_ID = 1

    def __init__(self) -> None:
        # Quiet llama at the source
        os.environ.setdefault("LLAMA_LOG_LEVEL", "50")  # 40=ERROR, 50=CRITICAL
        if callable(llama_log_set):
            try:
                def _drop_log(*_args, **_kwargs):
                    return

                try:
                    llama_log_set(_drop_log)  # new signature
                except TypeError:
                    llama_log_set(_drop_log, None)  # older signature
            except Exception:
                pass

        # Resolve model path: explicit env override wins; else pick largest .gguf under DOC_LLM_DIR
        mp = Path(DOC_LLM_GGUF) if DOC_LLM_GGUF else _find_gguf(DOC_LLM_DIR)
        if not mp or not mp.exists():
            raise FileNotFoundError(
                f"Docstring model not found. "
                f"Searched DOCSTRING_LLAMA_GGUF={DOC_LLM_GGUF!r} "
                f"and directory {DOC_LLM_DIR}"
            )
        self.MODEL_PATH = str(mp)

        print(f"[{self.__class__.__name__}] Loading model...")
        with _SuppressStdoutCaptureStderr() as cap:
            self.llm = Llama(
                model_path=self.MODEL_PATH,
                n_ctx=DOC_LLM_CTX,
                n_threads=8,
                use_mlock=True,
                verbose=False,  # suppress internal stdout spam
            )
        _print_stderr_summary("load", cap.read())

        # --- choose scan root at runtime (env wins) ---
        root_env = os.getenv("DOCSTRING_ROOT")
        chosen_root = root_env if root_env else self._DEFAULT_ROOT
        # reflect choice for logging/prints
        self.ROOT_DIR = chosen_root

        self.root_path = Path(chosen_root).resolve()
        if not self.root_path.is_dir():
            raise FileNotFoundError(f"Docstring scan root not found: {self.root_path}")

        os.environ.setdefault("SQLITE_DB_URL", f"sqlite:///{DB_PATH}")
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)  # ensure folder exists
        self.writer = DocstringWriter(agent_id=self.AGENT_ID, mode=self.WRITER_MODE)

        self.writer = DocstringWriter(agent_id=self.AGENT_ID, mode=self.WRITER_MODE)
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
            # Capture only stderr during inference; leave stdout alone.
            with _CaptureOnlyStderr() as cap:
                response = self.llm(prompt, max_tokens=64, stop=["\n"])
            _print_stderr_summary("infer", cap.read())

            # llama-cpp-python returns {'choices': [{'text': '...'}], ...}
            summary_text = (response.get("choices", [{}])[0].get("text") or "").strip()  # type: ignore[dict-item]
            return summary_text if summary_text else "Bad docstring"
        except Exception as e:
            print(f"[{self.__class__.__name__} ❌] LLM summarization failed: {e}")
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
            print(f"[{self.__class__.__name__} ❌] Failed to parse {filepath}: {e}")
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
                    "file_basename": filename,  # keep the original filename
                    "line": 1,
                    "language": language,
                    "class": "-",
                    "function": "-",
                    "summary": summary,
                    "_rel_path": rel_path,  # carry the real path explicitly
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
        h = hashlib.sha1(f"{filepath}|{symbol_type}|{symbol_name}|{lineno}".encode("utf-8")).hexdigest()
        return f"hash_{symbol_name}_{h[:10]}"

    @staticmethod
    def _to_writer_row(entry: dict) -> dict:
        """
        Adapt our extractor entry -> DocstringWriter's expected row shape for mode='introspection_index'.

        Writer expects:
          file -> becomes IntrospectionIndex.filepath (NOT NULL)
          filetype -> becomes symbol_type
          name/function/route choice -> name
          line -> lineno
          description -> description
          hash -> unique_key_hash
          ag_tag/status/route_method/route_path/target/relation supported
        """
        rel_path = (entry.get("_rel_path") or "").strip().replace("\\", "/")
        if not rel_path:
            raise ValueError("Missing _rel_path in entry")

        line = int(entry.get("line", 1))
        if line < 1:
            line = 1

        # Decide symbol_type and symbol_name for the writer
        cls = entry.get("class") or "-"
        func = entry.get("function") or "-"

        # FIX: treat ANY function (top-level or method) as 'function'
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

        # Build the exact row the writer expects
        row = {
            "file": rel_path,           # writer maps this to IntrospectionIndex.filepath
            "filetype": symbol_type,    # writer maps to symbol_type
            "line": line,               # writer maps to lineno
            "route_method": None,
            "route_path": None,
            "ag_tag": "AG-Introspection",
            "description": description,  # LLM one-liner
            "target": None,
            "relation": None,
            "hash": unique_hash,        # writer maps to unique_key_hash
            "status": "active",
            # name resolution fields:
            "function": None,
            "route": None,
            "name": None,
            # context (for debugging; writer ignores these in this mode)
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

        for dirpath, dirnames, filenames in os.walk(self.root_path):
            # Prune excluded dirs
            dirnames[:] = [d for d in dirnames if d not in self.EXCLUDED_DIRS]

            for filename in filenames:
                if not filename.endswith(".py"):
                    continue

                total_files += 1
                full_path = Path(dirpath) / filename
                rel_path = full_path.relative_to(self.root_path).as_posix()
                print(f"[{self.__class__.__name__}] Processing {rel_path}")

                try:
                    records = self.extract_docstrings(str(full_path))
                    for entry in records:
                        summary = (entry.get("summary") or "").strip()
                        if "LLM failed" in summary:
                            total_failed += 1
                            outcome = "LLM summarization failed"
                        elif "Duplicate" in summary:
                            total_skipped += 1
                            continue  # Skip writing
                        elif "Bad docstring" in summary:
                            outcome = "Too short or missing docstring"
                        else:
                            total_llm += 1
                            outcome = "Docstring summarized successfully"

                        writer_row = self._to_writer_row(entry)
                        # Write as a plain dict (this is what DocstringWriter expects)
                        self.writer.write(writer_row)
                        total_written += 1

                        # Mirror exactly what hits DB
                        symbol_type = writer_row["filetype"]
                        symbol_name = writer_row.get("function") or writer_row.get("route") or writer_row.get("name")
                        desc_snip = _one_line(writer_row["description"], 120)
                        print(
                            f"[{self.__class__.__name__} ✅] {writer_row['file']}:{writer_row['line']} "
                            f"{symbol_type}={symbol_name} — {outcome} — {desc_snip}"
                        )
                except Exception as e:
                    print(f"[{self.__class__.__name__} ❌] Error during processing {rel_path}: {e}")
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
    print(f"\n[{analyzer.__class__.__name__}] Starting docstring summarization")
    print(f"[{analyzer.__class__.__name__}] Root: {analyzer.root_path}")
    print(f"[{analyzer.__class__.__name__}] Model: {analyzer.MODEL_PATH}")
    print(f"[{analyzer.__class__.__name__}] Writer mode: {analyzer.WRITER_MODE}")

    start = time.time()
    stats = analyzer.traverse_and_write()
    duration = round(time.time() - start, 2)

    print(f"\n[{analyzer.__class__.__name__} ✅] Completed in {duration} sec")
    print(f"[{analyzer.__class__.__name__}] Files scanned: {stats['total_files']}")
    print(f"[{analyzer.__class__.__name__}] Records written: {stats['total_written']}")
    print(f"[{analyzer.__class__.__name__}] Duplicate skipped: {stats['total_skipped']}")
    print(f"[{analyzer.__class__.__name__} ❌] Parse/LLM failures: {stats['total_failed']}")
    print(f"[{analyzer.__class__.__name__}] LLM summaries created: {stats['total_llm']}")


