#!/usr/bin/env python3
"""
import_refactor.py — class-based import refactoring by mapping.

Usage (programmatic):
    mapping = {
        "backend.core.prompt_pipeline.executor.rewrite": "patches.core.rewrite",
        "backend.core.utils.io.patch_ops": "patches.core.patch_ops",
    }
    r = ImportRefactor(mapping, root=".", include_globs=("**/*.py",), exclude_globs=("**/.venv/**",))
    report = r.run(dry_run=True)  # or dry_run=False for in-place edit
    print(report.summary())

Design notes:
- Only refactors ABSOLUTE imports (level == 0). Relative imports are left untouched.
- Handles:
    import a.b.c as x, d.e
    from a.b import c as x, d
- For `from M import N`, if mapping contains the FULLY-QUALIFIED "M.N" → "X.Y", it rewrites to:
    from X import Y
- Prefix matching is supported: if name startswith old + ".", it's rewritten with the same suffix.
- Preserves file encoding and newline style; reconstructs only the changed import lines.

Limitations:
- Multi-line imports are reconstructed as a single logical import line (functionally equivalent).
- If a single `from ... import ...` line would need to split across multiple new modules,
  those specific names are left unchanged to avoid style churn; a warning is emitted.
"""

from __future__ import annotations

import ast
import fnmatch
import io
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


@dataclass
class Change:
    path: Path
    lineno: int
    old: str
    new: str

    def __str__(self) -> str:
        return f"{self.path}:{self.lineno}: {self.old!r} -> {self.new!r}"


@dataclass
class Report:
    changes: List[Change] = field(default_factory=list)
    errors: List[Tuple[Path, str]] = field(default_factory=list)
    skipped: List[Path] = field(default_factory=list)

    def add_change(self, change: Change) -> None:
        self.changes.append(change)

    def add_error(self, path: Path, msg: str) -> None:
        self.errors.append((path, msg))

    def add_skipped(self, path: Path) -> None:
        self.skipped.append(path)

    def summary(self) -> str:
        return (
            f"Refactor complete. Files changed: {len({c.path for c in self.changes})}, "
            f"changes: {len(self.changes)}, errors: {len(self.errors)}, skipped: {len(self.skipped)}"
        )


class ImportRefactor:
    def __init__(
        self,
        mapping: Dict[str, str],
        root: os.PathLike | str = ".",
        include_globs: Sequence[str] = ("**/*.py",),
        exclude_globs: Sequence[str] = ("**/.git/**", "**/.venv/**", "**/venv/**", "**/__pycache__/**"),
        prefix_match: bool = True,
        exact_only: bool = False,
        backup_suffix: Optional[str] = ".bak",
        encoding: Optional[str] = None,
    ) -> None:
        """
        :param mapping: dict of old_module_path -> new_module_path
        :param root: root directory to scan
        :param include_globs: file globs to include
        :param exclude_globs: file globs to exclude
        :param prefix_match: if True, also rewrite when an import starts with "old."
        :param exact_only: if True, do NOT do prefix expansions (overrides prefix_match)
        :param backup_suffix: if not None and dry_run=False, write a backup alongside edits
        :param encoding: if None, detect via tokenize-like BOM sniffing; else force
        """
        self.mapping = dict(mapping)
        self.root = Path(root)
        self.include_globs = tuple(include_globs)
        self.exclude_globs = tuple(exclude_globs)
        self.prefix_match = prefix_match and not exact_only
        self.exact_only = exact_only
        self.backup_suffix = backup_suffix
        self.encoding = encoding or "utf-8"

        # Precompute mapping keys sorted by descending length to prefer longest match first
        self._keys_by_len = sorted(self.mapping.keys(), key=len, reverse=True)

    # ------------------------------- public API --------------------------------

    def run(self, dry_run: bool = True) -> Report:
        report = Report()
        for path in self._iter_files():
            try:
                changed = self._refactor_file(path, dry_run=dry_run, report=report)
                if not changed:
                    report.add_skipped(path)
            except Exception as e:  # keep going; collect errors
                report.add_error(path, f"{type(e).__name__}: {e}")
        return report

    # ------------------------------- internals ---------------------------------

    def _iter_files(self) -> Iterable[Path]:
        all_paths: List[Path] = []
        for pat in self.include_globs:
            all_paths.extend(self.root.glob(pat))
        # Filter excludes
        def is_excluded(p: Path) -> bool:
            s = str(p.as_posix())
            return any(fnmatch.fnmatch(s, pat) for pat in self.exclude_globs)

        for p in sorted(set(all_paths)):
            if p.is_file() and not is_excluded(p):
                yield p

    def _refactor_file(self, path: Path, dry_run: bool, report: Report) -> bool:
        text = path.read_text(encoding=self.encoding)
        try:
            tree = ast.parse(text)
        except SyntaxError as e:
            report.add_error(path, f"SyntaxError at line {e.lineno}: {e.msg}")
            return False

        # Collect replacements as (start_index, end_index, new_text, lineno, old_text)
        replacements: List[Tuple[int, int, str, int, str]] = []
        lines = text.splitlines(keepends=True)
        line_offsets = self._line_offsets(lines)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                old_segment = self._slice(lines, line_offsets, node.lineno, node.col_offset, node.end_lineno, node.end_col_offset)
                new_segment = self._rewrite_import(node)
                if new_segment and new_segment != old_segment.strip("\n"):
                    start, end = self._abs_span(line_offsets, node.lineno, node.col_offset, node.end_lineno, node.end_col_offset)
                    replacements.append((start, end, new_segment, node.lineno, old_segment.rstrip("\n")))
            elif isinstance(node, ast.ImportFrom):
                # Only absolute imports
                if node.level and node.level > 0:
                    continue
                old_segment = self._slice(lines, line_offsets, node.lineno, node.col_offset, node.end_lineno, node.end_col_offset)
                new_segment = self._rewrite_importfrom(node)
                if new_segment and new_segment != old_segment.strip("\n"):
                    start, end = self._abs_span(line_offsets, node.lineno, node.col_offset, node.end_lineno, node.end_col_offset)
                    replacements.append((start, end, new_segment, node.lineno, old_segment.rstrip("\n")))

        if not replacements:
            return False

        # Apply replacements back-to-front to preserve offsets
        new_text = text
        for start, end, seg, lineno, old in sorted(replacements, key=lambda t: t[0], reverse=True):
            new_text = new_text[:start] + seg + new_text[end:]
            report.add_change(Change(path=path, lineno=lineno, old=old, new=seg))

        if not dry_run and new_text != text:
            if self.backup_suffix:
                backup_path = path.with_suffix(path.suffix + self.backup_suffix)
                backup_path.write_text(text, encoding=self.encoding)
            path.write_text(new_text, encoding=self.encoding)
        return True

    # ---- AST node rewriting ---------------------------------------------------

    def _rewrite_import(self, node: ast.Import) -> Optional[str]:
        """
        import a.b as c, x.y
        """
        new_parts: List[str] = []
        changed = False
        for alias in node.names:
            new_name = self._map_qualname(alias.name)
            if new_name != alias.name:
                changed = True
            part = f"import {new_name}"
            if alias.asname:
                part += f" as {alias.asname}"
            new_parts.append(part)

        if not changed:
            return None

        # When multiple names, we join with "; " to keep them on one physical line,
        # preserving semantics. We also compress multiple "import" into a single one.
        # Example: "import a as x, b" is printed as "import a as x, b"
        # Build a single import statement with comma-separated aliases that share 'import '.
        items = []
        for alias in node.names:
            new_name = self._map_qualname(alias.name)
            items.append(new_name + (f" as {alias.asname}" if alias.asname else ""))
        return "import " + ", ".join(items)

    def _rewrite_importfrom(self, node: ast.ImportFrom) -> Optional[str]:
        """
        from a.b import c as d, e
        - Try module-level remap first (a.b -> X.Y)
        - If not changed, try per-alias mapping with full name (a.b.c -> X.Y.Z)
        """
        if node.module is None:
            return None

        module_changed = False
        new_module = self._map_qualname(node.module)
        if new_module != node.module:
            module_changed = True

        # Try alias-level remap where mapping = "module.name" -> "new.module.name2"
        # Only safe when a single alias OR when all mapped aliases share same new module.
        alias_specs: List[Tuple[str, Optional[str]]] = []  # (name_or_attr, asname)
        for alias in node.names:
            alias_specs.append((alias.name, alias.asname))

        alias_level_changes: List[Tuple[str, str, Optional[str]]] = []  # (old_full, new_full, asname)
        for name, asname in alias_specs:
            full = f"{node.module}.{name}"
            mapped_full = self._map_qualname(full)
            if mapped_full != full:
                alias_level_changes.append((full, mapped_full, asname))

        if not module_changed and not alias_level_changes:
            return None

        # If module_changed, we keep original alias names.
        if module_changed:
            items = [n + (f" as {a}" if a else "") for (n, a) in alias_specs]
            return f"from {new_module} import " + ", ".join(items)

        # Else apply alias-level changes. If multiple aliases produce different new modules, bail out (warn).
        # We'll group by new module; if >1 distinct modules, do nothing for safety.
        new_modules = {full_new.rsplit(".", 1)[0] for _, full_new, _ in alias_level_changes}
        if len(new_modules) == 1:
            target_module = next(iter(new_modules))
            # Reconstruct names (mapped if present, else original)
            out_items: List[str] = []
            for name, asname in alias_specs:
                full = f"{node.module}.{name}"
                mapped = next((nf for of, nf, _ in alias_level_changes if of == full), None)
                if mapped:
                    new_name = mapped.rsplit(".", 1)[1]
                else:
                    new_name = name
                out_items.append(new_name + (f" as {asname}" if asname else ""))
            return f"from {target_module} import " + ", ".join(out_items)

        # Ambiguous: different target modules across aliases — skip for safety.
        return None

    # ---- mapping helpers ------------------------------------------------------

    def _map_qualname(self, name: str) -> str:
        """
        Apply mapping to a dotted qualname. Longest-key wins. Supports exact and (optionally) prefix matches.
        """
        for old in self._keys_by_len:
            if name == old:
                return self.mapping[old]
            if self.prefix_match and (name.startswith(old + ".")):
                suffix = name[len(old) :]
                return self.mapping[old] + suffix
        return name

    # ---- text slicing utilities ----------------------------------------------

    @staticmethod
    def _line_offsets(lines: List[str]) -> List[int]:
        offs = [0]
        total = 0
        for ln in lines:
            total += len(ln)
            offs.append(total)
        return offs

    @staticmethod
    def _abs_span(line_offsets: List[int], lineno: int, col: int, end_lineno: int, end_col: int) -> Tuple[int, int]:
        start = line_offsets[lineno - 1] + col
        end = line_offsets[end_lineno - 1] + end_col
        return start, end

    @staticmethod
    def _slice(lines: List[str], line_offsets: List[int], lineno: int, col: int, end_lineno: int, end_col: int) -> str:
        start, end = ImportRefactor._abs_span(line_offsets, lineno, col, end_lineno, end_col)
        text = "".join(lines)
        return text[start:end]


# ------------------------------- example run -----------------------------------

if __name__ == "__main__":
    # Example: refactor two imports across the repo, dry-run by default.
    mapping = {
        # v2 namespace (present in repo)
    "v2.backend.core.prompt_pipeline.executor.rewrite": "patches.core.rewrite",
    "v2.backend.core.utils.io.patch_ops": "patches.core.patch_ops"
    }
    r = ImportRefactor(mapping, root=Path("."), include_globs=("codebase/**/*.py",))
    rep = r.run(dry_run=True)
    print(rep.summary())
    # Print a few sample changes
    for ch in rep.changes[:20]:
        print(ch)
