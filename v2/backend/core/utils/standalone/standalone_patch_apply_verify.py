#!/usr/bin/env python3
"""
Standalone Patch Apply + Static Verify (no CLI/env; edit variables below)
------------------------------------------------------------------------
- Applies unified diff patches to REPO_ROOT (staged copy by default).
- Static checks: Python AST parse + fingerprint, JSON/TOML/YAML parse,
  optional SQLite script exec for .sql files.
- Emits a JSON report with per-file pre/post SHA-256 and validation results.
"""
from __future__ import annotations
import hashlib, json, os, re, shutil, sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple, Optional, Dict

# ---------------------- USER SETTINGS (EDIT THESE) ----------------------
REPO_ROOT = Path(r"C:\Users\cg371\PycharmProjects\ChatGPT Bot\v2\backend\core\utils\code_bundles")  # <-- EDIT
PATCH_FILES = [
    Path(r"C:\Users\cg371\PycharmProjects\ChatGPT Bot\v2\backend\core\utils\standalone\wire_python_ast_plugin.patch"),               # <-- EDIT
]
INPLACE = False
STAGING_COPY = Path(r"C:\Users\cg371\PycharmProjects\ChatGPT Bot\v2\backend\core\utils\standalone\ChatGPT_Bot_STAGED")
BACKUP_EXT = ".bak"     # used only when INPLACE=True
REPORT_PATH = Path(r"C:\Users\cg371\PycharmProjects\ChatGPT Bot\v2\backend\core\utils\standalone\patch_report.json")
SQLCHECK = "none"       # "none" | "sqlite"
# -----------------------------------------------------------------------

try:
    import tomllib  # py311+
except Exception:
    tomllib = None  # type: ignore
try:
    import yaml  # optional
except Exception:
    yaml = None  # type: ignore

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

@dataclass
class HunkLine:
    tag: str  # ' ', '+', '-'
    text: str

@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: List[HunkLine]

@dataclass
class FilePatch:
    old_path: str
    new_path: str
    hunks: List[Hunk]
    is_new: bool = False
    is_deleted: bool = False

_HUNK_HEADER = re.compile(r"^@@ -(?P<o_start>\d+)(,(?P<o_count>\d+))? \+(?P<n_start>\d+)(,(?P<n_count>\d+))? @@")

def parse_unified_diff(diff_text: str) -> List[FilePatch]:
    lines = diff_text.splitlines()
    i = 0
    patches: List[FilePatch] = []

    def read_file_header(idx: int):
        old_path = None
        new_path = None
        while idx < len(lines):
            line = lines[idx]
            if line.startswith('--- '):
                old_path = line[4:].strip()
                idx += 1
                if idx < len(lines) and lines[idx].startswith('+++ '):
                    new_path = lines[idx][4:].strip()
                    idx += 1
                break
            idx += 1
        return idx, old_path, new_path

    def clean(p: Optional[str]) -> Optional[str]:
        if p is None:
            return None
        if p.startswith('a/') or p.startswith('b/'):
            return p[2:]
        return p

    while i < len(lines):
        i, old_p, new_p = read_file_header(i)
        if not old_p and not new_p:
            break
        old_p = clean(old_p)
        new_p = clean(new_p)
        is_new = old_p in (None, '/dev/null', 'dev/null')
        is_deleted = new_p in (None, '/dev/null', 'dev/null')
        if old_p is None:
            old_p = new_p
        if new_p is None:
            new_p = old_p

        hunks: List[Hunk] = []
        while i < len(lines) and lines[i].startswith('@@ '):
            m = _HUNK_HEADER.match(lines[i])
            if not m:
                raise ValueError(f"Malformed hunk header: {lines[i]}")
            i += 1
            o_start = int(m.group('o_start'))
            o_count = int(m.group('o_count') or '1')
            n_start = int(m.group('n_start'))
            n_count = int(m.group('n_count') or '1')
            h_lines: List[HunkLine] = []
            while i < len(lines):
                if lines[i].startswith('@@ '):
                    break
                if lines[i].startswith('--- ') and (i+1 < len(lines) and lines[i+1].startswith('+++ ')):
                    break
                if lines[i].startswith('\\ No newline at end of file'):
                    i += 1
                    continue
                if lines[i] == "":
                    h_lines.append(HunkLine(' ', ''))
                    i += 1
                    continue
                tag = lines[i][0]
                if tag not in (' ', '+', '-'):
                    tag = ' '
                    text = lines[i]
                else:
                    text = lines[i][1:]
                h_lines.append(HunkLine(tag, text))
                i += 1
            hunks.append(Hunk(o_start, o_count, n_start, n_count, h_lines))

        patches.append(FilePatch(old_p or "", new_p or "", hunks, is_new, is_deleted))

    return patches

def apply_hunks_to_text(src: str, hunks: List[Hunk]):
    src_lines = src.splitlines()
    offset = 0
    for h in hunks:
        o_start0 = h.old_start - 1 + offset
        o_end0 = o_start0 + h.old_count
        expected_old = [ln.text for ln in h.lines if ln.tag in (' ', '-')]
        actual_old = src_lines[o_start0:o_end0]
        if actual_old != expected_old:
            return src, False
        new_slice = [ln.text for ln in h.lines if ln.tag in (' ', '+')]
        src_lines[o_start0:o_end0] = new_slice
        offset += (h.new_count - h.old_count)
    return "\\n".join(src_lines), True

def ast_fingerprint(py_text: str) -> str:
    import ast
    tree = ast.parse(py_text)
    dump = ast.dump(tree, annotate_fields=False, include_attributes=False)
    return sha256_bytes(dump.encode('utf-8'))

def validate_text(path: Path, text: str, sqlcheck: str = 'none') -> Dict[str, object]:
    p = str(path).lower()
    out: Dict[str, object] = {}
    if p.endswith('.py'):
        ok = True
        err = ''
        ast_hash = ''
        try:
            ast_hash = ast_fingerprint(text)
        except Exception as e:
            ok = False
            err = f"python_ast_error: {e}"
        out.update({"python_ok": ok, "python_ast_hash": ast_hash, "python_error": err})
    elif p.endswith('.json'):
        try:
            json.loads(text)
            out.update({"json_ok": True})
        except Exception as e:
            out.update({"json_ok": False, "json_error": str(e)})
    elif p.endswith('.toml') and tomllib is not None:
        try:
            tomllib.loads(text)  # type: ignore[arg-type]
            out.update({"toml_ok": True})
        except Exception as e:
            out.update({"toml_ok": False, "toml_error": str(e)})
    elif p.endswith(('.yaml', '.yml')):
        if yaml is not None:
            try:
                yaml.safe_load(text)  # type: ignore[call-arg]
                out.update({"yaml_ok": True})
            except Exception as e:
                out.update({"yaml_ok": False, "yaml_error": str(e)})
        else:
            out.update({"yaml_ok": None, "yaml_warning": "PyYAML not installed; skipped"})
    elif p.endswith('.sql'):
        paren_bal = text.count('(') == text.count(')')
        out.update({"sql_paren_balanced": paren_bal})
        if sqlcheck.lower() == 'sqlite':
            try:
                con = sqlite3.connect(':memory:')
                con.executescript(text)
                con.close()
                out.update({"sqlite_ok": True})
            except Exception as e:
                out.update({"sqlite_ok": False, "sqlite_error": str(e)})
    else:
        out.update({"text_checked": True})
    return out

def ensure_within_root(path: Path, root: Path) -> Path:
    p = path.resolve()
    r = root.resolve()
    if not str(p).startswith(str(r)):
        raise ValueError(f"Path escapes repo root: {p} not under {r}")
    return p

def apply_patches(repo_root: Path, patch_files: list[Path], staging: Optional[Path], inplace: bool,
                  backup_ext: Optional[str], sqlcheck: str) -> Dict[str, object]:
    if staging and not inplace:
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(repo_root, staging)
        work_root = staging.resolve()
    else:
        work_root = repo_root.resolve()

    results = {"repo_root": str(repo_root), "work_root": str(work_root), "inplace": inplace, "changes": [], "errors": []}

    for patch_path in patch_files:
        diff_text = patch_path.read_text(encoding='utf-8', errors='replace')
        file_patches = parse_unified_diff(diff_text)
        for fp in file_patches:
            target_rel = fp.new_path if not fp.is_deleted else fp.old_path
            target_abs = ensure_within_root(work_root / target_rel, work_root)
            target_abs.parent.mkdir(parents=True, exist_ok=True)

            if fp.is_deleted:
                if target_abs.exists():
                    orig_bytes = target_abs.read_bytes()
                    orig_text = orig_bytes.decode('utf-8', errors='replace')
                    new_text, ok = apply_hunks_to_text(orig_text, fp.hunks)
                    if not ok:
                        results["errors"].append({"file": target_rel, "error": "context_mismatch_on_delete"})
                        continue
                    if backup_ext and inplace:
                        target_abs.with_suffix(target_abs.suffix + backup_ext).write_bytes(orig_bytes)
                    target_abs.unlink()
                    results["changes"].append({"file": target_rel, "action": "delete",
                                               "pre_sha256": sha256_bytes(orig_bytes), "post_sha256": None})
                else:
                    results["errors"].append({"file": target_rel, "error": "delete_missing_file"})
                continue

            pre_bytes = b''
            pre_sha = None
            if target_abs.exists():
                pre_bytes = target_abs.read_bytes()
                pre_sha = sha256_bytes(pre_bytes)
                pre_text = pre_bytes.decode('utf-8', errors='replace')
            else:
                pre_text = ''

            if fp.hunks:
                new_text, ok = apply_hunks_to_text(pre_text, fp.hunks)
                if not ok:
                    results["errors"].append({"file": target_rel, "error": "context_mismatch"})
                    continue
            else:
                new_lines = []
                for h in fp.hunks:
                    new_lines.extend([ln.text for ln in h.lines if ln.tag in (' ', '+')])
                new_text = "\\n".join(new_lines)

            val = validate_text(target_abs, new_text, sqlcheck=sqlcheck)
            post_bytes = new_text.encode('utf-8')
            post_sha = sha256_bytes(post_bytes)

            if backup_ext and target_abs.exists() and inplace:
                target_abs.with_suffix(target_abs.suffix + backup_ext).write_bytes(pre_bytes)
            target_abs.write_bytes(post_bytes)

            results["changes"].append({"file": target_rel, "action": "create" if not pre_sha else "modify",
                                       "pre_sha256": pre_sha, "post_sha256": post_sha, "validation": val})

    return results

def run():
    repo_root = REPO_ROOT
    patch_files = [Path(p) for p in PATCH_FILES]
    staging = None if INPLACE else STAGING_COPY
    results = apply_patches(repo_root, patch_files, staging, INPLACE, BACKUP_EXT, SQLCHECK)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(results, indent=2), encoding='utf-8')
    print(f"Applied with {len(results['changes'])} change(s), {len(results['errors'])} error(s).")
    print(f"Report: {REPORT_PATH}")

if __name__ == "__main__":
    run()
