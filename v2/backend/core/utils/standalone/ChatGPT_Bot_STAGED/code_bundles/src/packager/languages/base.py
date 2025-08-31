# File: backend/core/utils/code_bundles/code_bundles_v2/src/packager/languages/base.py
from __future__ import annotations

"""
Minimal plugin contract + loader for language analyzers.

A language plugin MUST expose either:
  - a module-level object named `PLUGIN` with attributes:
        name: str
        extensions: tuple[str, ...]   # e.g. (".py", ".pyi")
        analyze(files: list[tuple[str, bytes]]) -> dict[str, object]
  - OR a `PythonAnalyzer`-style class with `.analyze(files)` and we will wrap it,
    inferring extensions from a module-level tuple `EXTENSIONS`.

No external deps; stdlib only.
"""

from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable, List, Optional, Sequence, Tuple
import importlib
import sys


@dataclass(frozen=True)
class LoadedPlugin:
    name: str
    extensions: Tuple[str, ...]
    analyze: Any  # callable(files: list[tuple[str, bytes]]) -> dict[str, Any]


def _find_languages_dir() -> Optional[Path]:
    """
    Locate the 'languages' package directory relative to this file.
    Expected layout: .../src/packager/languages/
    """
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "languages").is_dir() and (p / "core").is_dir():
            return p / "languages"
    # Fallback: search up to repo root
    for p in here.parents:
        cand = p / "languages"
        if cand.is_dir():
            return cand
    return None


def _module_for_language(dirname: str) -> Optional[ModuleType]:
    """
    Try to import '<pkg>.languages.<dirname>.plugin' using both absolute and relative roots.
    """
    module_names = [
        f"packager.languages.{dirname}.plugin",
        f"{__package__.rsplit('.languages', 1)[0]}.languages.{dirname}.plugin" if __package__ else None,
    ]
    for mod in module_names:
        if not mod:
            continue
        try:
            return importlib.import_module(mod)
        except Exception:
            continue
    # Last chance: try importing by file path
    langs_dir = _find_languages_dir()
    if not langs_dir:
        return None
    plugin_py = langs_dir / dirname / "plugin.py"
    if not plugin_py.is_file():
        return None
    spec = importlib.util.spec_from_file_location(f"packager.languages.{dirname}.plugin", str(plugin_py))
    if spec and spec.loader:
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)  # type: ignore[attr-defined]
        return mod
    return None


def _wrap_legacy(mod: ModuleType) -> Optional[LoadedPlugin]:
    """
    Wrap a legacy analyzer module that exposes `PythonAnalyzer` (or similar)
    and an `EXTENSIONS` tuple like (".py", ".pyi").
    """
    EXT = getattr(mod, "EXTENSIONS", None)
    if not (isinstance(EXT, (tuple, list)) and all(isinstance(x, str) for x in EXT)):
        return None
    Analyzer = getattr(mod, "PythonAnalyzer", None)
    if Analyzer is None:
        return None

    def analyze(files: List[Tuple[str, bytes]]):
        return Analyzer().analyze(files)

    name = getattr(mod, "PLUGIN_NAME", None) or "python"
    return LoadedPlugin(name=name, extensions=tuple(EXT), analyze=analyze)


def _load_from_module(mod: ModuleType) -> Optional[LoadedPlugin]:
    plug = getattr(mod, "PLUGIN", None)
    if plug is not None:
        name = getattr(plug, "name", None)
        exts = tuple(getattr(plug, "extensions", ()))
        analyze = getattr(plug, "analyze", None)
        if isinstance(name, str) and exts and callable(analyze):
            return LoadedPlugin(name=name, extensions=exts, analyze=analyze)
    # Try legacy wrapping
    return _wrap_legacy(mod)


def discover_language_plugins() -> List[LoadedPlugin]:
    """
    Discover all language plugins under 'languages/*/plugin.py'.
    Returns a list of LoadedPlugin entries. Order is deterministic (by dirname).
    """
    langs_dir = _find_languages_dir()
    if not langs_dir:
        return []

    plugins: List[LoadedPlugin] = []
    for d in sorted([p for p in langs_dir.iterdir() if p.is_dir() and (p / "plugin.py").is_file()],
                    key=lambda p: p.name):
        mod = _module_for_language(d.name)
        if not mod:
            continue
        lp = _load_from_module(mod)
        if lp:
            plugins.append(lp)
    return plugins
