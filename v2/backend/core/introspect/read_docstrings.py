#v2\backend\core\introspect\read_docstrings.py
from __future__ import annotations
r"""
Docstring reader/summarizer that walks a Python tree, summarizes docstrings via
llama.cpp (local Mistral .gguf), and writes rows using DocstringWriter to the
'introspection_index' table.

Updates in this version:
- Honors BOTH `exclude_globs` (full path patterns) and `segment_excludes`
  (directory basenames) exactly as provided by config\packager.yml.
- Robust matching: applies glob patterns against repo-relative POSIX paths;
  also prunes dirnames by case-insensitive basename.
- No env for DB; writer uses YAML-backed engine from db.access.db_init.

Spine capability: introspect.docstrings.scan.v1 → run_v1(task, context)
"""

import ast
import os
import time
import tempfile
import fnmatch
from pathlib import Path
from typing import List, Dict, Any, Optional, Iterable, Tuple

from llama_cpp import Llama  # type: ignore

from v2.backend.core.db.writers.docstring_writer import DocstringWriter
from v2.backend.core.spine.contracts import Artifact, Task


# ----------------------------- low-level IO helpers ---------------------------

def _stderr_to_oneliner(text: str, max_len: int = 220) -> str:
    clean = " ".join((text or "").strip().split())
    return clean[:max_len] + ("…" if len(clean) > max_len else "")


def _print_stderr_summary(stage: str, stderr_bytes: bytes) -> None:
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
    """Redirect C-level stdout to null and capture stderr to a temp file."""
    def __init__(self) -> None:
        self._devnull_fd = None
        self._old_stdout_fd = None
        self._old_stderr_fd = None
        self._stderr_tmp = None

    def __enter__(self):
        self._devnull_fd = os.open(os.devnull, os.O_WRONLY)
        self._old_stdout_fd = os.dup(1)
        self._old_stderr_fd = os.dup(2)
        os.dup2(self._devnull_fd, 1)
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
    """Capture C-level stderr to a temp file while leaving stdout alone."""
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


# ----------------------------- glob/exclude helpers ---------------------------

def _as_posix(s: str) -> str:
    return s.replace("\\", "/")

def _both_sep(pat: str) -> Tuple[str, str]:
    return (pat.replace("\\", "/"), pat.replace("/", "\\"))

def _path_matches_any(rel_posix: str, patterns: Iterable[str]) -> bool:
    """Match rel_posix (already POSIX) against glob patterns (check both sep variants)."""
    for pat in patterns:
        p1, p2 = _both_sep(str(pat))
        if fnmatch.fnmatch(rel_posix, _as_posix(p1)) or fnmatch.fnmatch(rel_posix, _as_posix(p2)):
            return True
    return False

def _dirnames_from_globs(globs: Iterable[str]) -> List[str]:
    out: List[str] = []
    for g in globs or []:
        g = _as_posix(str(g))
        if g.endswith("/**"):
            g = g[:-3]
        seg = g.split("/")[-1].strip()
        if seg and "*" not in seg and "?" not in seg and seg not in out:
            out.append(seg)
    return out


# ----------------------------- analyzer ---------------------------------------

class DocStringAnalyzer:
    """
    Summarizes module/class/function docstrings using llama.cpp and writes rows
    via DocstringWriter.

    Parameters:
      root_path       : Path to scan (repository root)
      model_path      : .gguf file to load
      ctx             : llama n_ctx (default 2048)
      threads         : llama n_threads (default 8)
      gpu_layers      : llama n_gpu_layers (default 0)
      use_mlock       : whether to use mlock (default False on Windows)
      status_default  : status to write into DB rows (e.g., "todo"|"active")
      prune_basenames : set of directory basenames to prune (case-insensitive)
      exclude_globs   : full-path glob patterns to exclude (repo-relative POSIX)
      char_threshold  : minimum docstring length; below → "Bad docstring"
      max_tokens      : llama max_tokens for summary
    """

    def __init__(
        self,
        *,
        root_path: Path,
        model_path: Path,
        ctx: int = 2048,
        threads: int = 8,
        gpu_layers: int = 0,
        use_mlock: bool = False,
        status_default: str = "active",
        prune_basenames: Optional[Iterable[str]] = None,
        exclude_globs: Optional[Iterable[str]] = None,
        char_threshold: int = 50,
        max_tokens: int = 64,
    ) -> None:
        os.environ["LLAMA_LOG_LEVEL"] = "60"  # silence noisy C callback logs

        self.root_path = Path(root_path).resolve()
        if not self.root_path.is_dir():
            raise FileNotFoundError(f"Docstring scan root not found: {self.root_path}")

        self.model_path = Path(model_path).resolve()
        if not self.model_path.is_file():
            raise FileNotFoundError(f"Docstring model not found: {self.model_path}")

        self.ctx = int(ctx)
        self.threads = int(threads)
        self.gpu_layers = int(gpu_layers)
        self.use_mlock = bool(use_mlock)
        self.status_default = str(status_default or "active")
        self.char_threshold = int(char_threshold)
        self.max_tokens = int(max_tokens)

        base_ex = {"__pycache__", "venv", "env", ".git", "site-packages"}
        extra = {e.strip() for e in (prune_basenames or []) if isinstance(e, str) and e.strip()}
        self.prune_basenames = {*(d for d in base_ex), *extra}
        self.prune_basenames_lower = {d.lower() for d in self.prune_basenames}

        self.exclude_globs = tuple(exclude_globs or ())

        size_mb = (self.model_path.stat().st_size or 0) / (1024 * 1024)
        print(
            f"[DocStringAnalyzer] Loading model {self.model_path.name} ({size_mb:.1f} MB) "
            f"| ctx={self.ctx} | threads={self.threads} | mlock={'on' if self.use_mlock else 'off'} | gpu_layers={self.gpu_layers}"
        )

        t0 = time.time()
        with _SuppressStdoutCaptureStderr() as cap:
            self.llm = Llama(
                model_path=str(self.model_path),
                n_ctx=self.ctx,
                n_threads=self.threads,
                n_gpu_layers=self.gpu_layers,
                use_mlock=self.use_mlock,
                verbose=False,
            )
        _print_stderr_summary("load", cap.read())
        print(f"[DocStringAnalyzer] Model loaded in {time.time() - t0:.2f}s")

        self.writer = DocstringWriter(agent_id=1, mode="introspection_index")
        self.seen_docstrings: set[str] = set()

    # ----- AST helpers -----

    @staticmethod
    def _annotate_parents(tree: ast.AST) -> None:
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                child.parent = node  # type: ignore[attr-defined]

    @staticmethod
    def _enclosing_class_name(func_node: ast.AST) -> Optional[str]:
        parent = getattr(func_node, "parent", None)
        while parent:
            if isinstance(parent, ast.ClassDef):
                return parent.name
            parent = getattr(parent, "parent", None)
        return None

    # ----- LLM summarization -----

    def summarize_docstring(self, docstring: Optional[str]) -> str:
        text = (docstring or "").strip()
        if len(text) <= self.char_threshold:
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
                response = self.llm(prompt, max_tokens=self.max_tokens, stop=["\n"])
            _print_stderr_summary("infer", cap.read())
            summary_text = (response.get("choices", [{}])[0].get("text") or "").strip()  # type: ignore[dict-item]
            return summary_text if summary_text else "Bad docstring"
        except Exception as e:
            print(f"[DocStringAnalyzer ❌] LLM summarization failed: {e}")
            return "LLM failed"

    # ----- Extraction -----

    def extract_docstrings(self, filepath: Path) -> List[Dict[str, Any]]:
        try:
            src = filepath.read_text(encoding="utf-8")
            tree = ast.parse(src, filename=str(filepath))
            self._annotate_parents(tree)
        except Exception as e:
            rel = filepath.relative_to(self.root_path).as_posix()
            print(f"[DocStringAnalyzer ❌] Failed to parse {rel}: {e}")
            return []

        rel_path = filepath.relative_to(self.root_path).as_posix()
        subdir = (filepath.parent.relative_to(self.root_path).as_posix() if filepath.parent != self.root_path else "")
        filename = filepath.name
        language = "Python" if filename.endswith(".py") else "Unknown"

        entries: List[Dict[str, Any]] = []

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
                        "line": int(getattr(node, "lineno", 1) or 1),
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
                cname = self._enclosing_class_name(node)
                entries.append(
                    {
                        "subdir": subdir,
                        "file_basename": filename,
                        "line": int(getattr(node, "lineno", 1) or 1),
                        "language": language,
                        "class": cname or "-",
                        "function": node.name,
                        "summary": summary,
                        "_rel_path": rel_path,
                    }
                )

        return entries

    # ----- Writer adapter -----

    @staticmethod
    def _hash_for_writer(filepath: str, symbol_type: str, symbol_name: str, lineno: int) -> str:
        import hashlib as _h
        h = _h.sha1(f"{filepath}|{symbol_type}|{symbol_name}|{lineno}".encode("utf-8")).hexdigest()
        return f"hash_{symbol_name}_{h[:10]}"

    def _to_writer_row(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        rel_path = (entry.get("_rel_path") or "").strip().replace("\\", "/")
        line = int(entry.get("line") or 1) or 1

        cls = entry.get("class") or "-"
        func = entry.get("function") or "-"

        if func != "-":
            symbol_type = "function"
            symbol_name = func
            name_key = "function"
        elif cls != "-":
            symbol_type = "class"
            symbol_name = cls
            name_key = "name"
        else:
            symbol_type = "module"
            symbol_name = Path(entry.get("file_basename", "")).stem or "unknown"
            name_key = "name"

        description = (entry.get("summary") or "").strip()
        unique_hash = self._hash_for_writer(rel_path, symbol_type, symbol_name, line)

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
            "status": self.status_default,
            "function": None,
            "route": None,
            "name": None,
            "subdir": entry.get("subdir", ""),
            "analyzer": "DocStringAnalyzer",
        }
        row[name_key] = symbol_name
        return row

    # ----- Traverse & write -----

    def traverse_and_write(self) -> Dict[str, int]:
        total_files = 0
        total_written = 0
        total_skipped = 0
        total_failed = 0
        total_llm = 0

        print(f"[DocStringAnalyzer] Starting traversal at {self.root_path}")
        prune = self.prune_basenames
        prune_lower = self.prune_basenames_lower
        patterns = self.exclude_globs

        for dirpath, dirnames, filenames in os.walk(self.root_path):
            rel_dir = Path(dirpath).relative_to(self.root_path).as_posix() if Path(dirpath) != self.root_path else ""

            keep_dirs: List[str] = []
            for d in dirnames:
                if d in prune or d.lower() in prune_lower:
                    continue
                rel_candidate = (Path(rel_dir) / d).as_posix() if rel_dir else d
                if _path_matches_any(rel_candidate, patterns) or _path_matches_any(f"{rel_candidate}/", patterns):
                    continue
                keep_dirs.append(d)
            dirnames[:] = keep_dirs

            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                rel_file = (Path(rel_dir) / filename).as_posix() if rel_dir else filename
                if _path_matches_any(rel_file, patterns):
                    continue

                total_files += 1
                full_path = Path(dirpath) / filename
                rel_path = full_path.relative_to(self.root_path).as_posix()
                print(f"[DocStringAnalyzer] Processing {rel_path}")
                try:
                    records = self.extract_docstrings(full_path)
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

                        row = self._to_writer_row(entry)
                        self.writer.write(row)
                        total_written += 1

                        symbol_type = row["filetype"]
                        symbol_name = row.get("function") or row.get("route") or row.get("name")
                        desc_snip = " ".join((row["description"] or "").split())[:120]
                        if len((row["description"] or "")) > 120:
                            desc_snip += "…"
                        print(
                            f"[DocStringAnalyzer ✅] {row['file']}:{row['line']} "
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


# ----------------------------- Spine target -----------------------------------

def _problem(code: str, message: str) -> List[Artifact]:
    return [
        Artifact(
            kind="Problem",
            uri="spine://problem/introspect.docstrings.scan.v1",
            sha256="",
            meta={"problem": {"code": code, "message": message, "retryable": False, "details": {}}},
        )
    ]


def _result(meta: Dict[str, Any]) -> List[Artifact]:
    return [
        Artifact(
            kind="Result",
            uri="spine://result/introspect.docstrings.scan.v1",
            sha256="",
            meta=meta,
        )
    ]


def run_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    """
    Payload keys (all required):
      sqlalchemy_url (str)      : DB URL (e.g., sqlite:///C:/.../bot_dev.db)
      sqlalchemy_table (str)    : must be 'introspection_index'
      scan_root (str)           : repo root to scan
      exclude_globs (list[str]) : file/dir patterns to exclude (full-path)
      segment_excludes (list[str]): directory basenames to prune (case-insensitive)
      status (str)              : status to write to DB rows
      model_path (str)          : .gguf model absolute path
    """
    p = task.payload or {}
    try:
        url = str(p.get("sqlalchemy_url") or "").strip()
        table = str(p.get("sqlalchemy_table") or "").strip()
        scan_root = str(p.get("scan_root") or "").strip()
        exclude_globs = list(p.get("exclude_globs") or [])
        segment_excludes = list(p.get("segment_excludes") or [])
        status = str(p.get("status") or "").strip() or "active"
        model_path = str(p.get("model_path") or "").strip()

        if not url:
            return _problem("InvalidPayload", "Missing 'sqlalchemy_url'.")
        if table != "introspection_index":
            return _problem("InvalidPayload", "sqlalchemy_table must be 'introspection_index'.")
        if not scan_root:
            return _problem("InvalidPayload", "Missing 'scan_root'.")
        if not model_path:
            return _problem("InvalidPayload", "Missing 'model_path' (.gguf).")

        root = Path(scan_root).resolve()
        mp = Path(model_path).resolve()
        if not root.is_dir():
            return _problem("ValidationError", f"scan_root not found: {root}")
        if not mp.is_file():
            return _problem("ValidationError", f"model_path not found: {mp}")

        prune_basenames = set(segment_excludes) | set(_dirnames_from_globs(exclude_globs))

        analyzer = DocStringAnalyzer(
            root_path=root,
            model_path=mp,
            ctx=8192,                 # increased context size
            threads=8,
            gpu_layers=0,
            use_mlock=False if os.name == "nt" else True,
            status_default=status,
            prune_basenames=prune_basenames,
            exclude_globs=exclude_globs,
            char_threshold=50,
            max_tokens=64,
        )

        print(f"[DocStringAnalyzer] Using model: {analyzer.model_path}")
        print(f"[DocStringAnalyzer] Dir prune basenames: {sorted({*prune_basenames})}")
        print(f"[DocStringAnalyzer] Exclude globs: {list(exclude_globs)}")

        t0 = time.time()
        stats = analyzer.traverse_and_write()
        dur = round(time.time() - t0, 2)

        return _result(
            {
                "files_scanned": int(stats.get("total_files", 0)),
                "records_written": int(stats.get("total_written", 0)),
                "duplicates_skipped": int(stats.get("total_skipped", 0)),
                "parse_or_llm_failures": int(stats.get("total_failed", 0)),
                "llm_summaries": int(stats.get("total_llm", 0)),
                "duration_sec": dur,
                "status": "ok",
            }
        )
    except Exception as e:
        return _problem("UnhandledError", f"{type(e).__name__}: {e}")


__all__ = ["run_v1"]




