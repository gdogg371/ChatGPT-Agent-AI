from __future__ import annotations

from pathlib import Path


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def analysis_out_dir(repo_root: Path, emitted_prefix: str) -> Path:
    """
    Compute analysis output dir from emitted_prefix (repo-relative).
    """
    out = (repo_root / emitted_prefix / "analysis").resolve()
    ensure_dir(out)
    return out
