# v2/backend/core/utils/code_bundles/code_bundles/run_pack.py
from __future__ import annotations

import argparse
import sys
import json
import fnmatch
import inspect
from pathlib import Path
from types import SimpleNamespace as NS

# Ensure we import the LOCAL packager from ./src (force it ahead of site-packages)
ROOT = Path(__file__).parent
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from packager.core.orchestrator import Packager  # <-- always local now
import packager.core.orchestrator as orch_mod     # for provenance print

# --- Hard-coded paths ----------------------------------------------------------
# GitHub code is staged/mirrored here (the "codebase" mirror, now relocated)
DEFAULT_SRC    = Path(r"C:\Users\cg371\PycharmProjects\ChatGPT Bot\output\patch_code_bundles")
DEFAULT_OUT    = Path(r"C:\Users\cg371\PycharmProjects\ChatGPT Bot\output\design_manifest")
DEFAULT_INGEST = Path(r"C:\Users\cg371\PycharmProjects\ChatGPT Bot\v2")
DEFAULT_SECRET = Path(r"C:\Users\cg371\PycharmProjects\ChatGPT Bot\secret_management\publish.local.json")

# --- Tiny holder classes to avoid config import mismatches ---------------------
class Transport(NS): pass
class GitHubTarget(NS): pass
class PublishOptions(NS): pass


def _bool(x, default=False) -> bool:
    if isinstance(x, bool):
        return x
    if isinstance(x, str):
        return x.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(x, (int, float)):
        return bool(x)
    return default


def _load_secrets(p: Path) -> dict:
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _under_allowed_mirror(src: Path) -> bool:
    """Return True iff src is (or is inside) output/patch_code_bundles."""
    p = src.resolve().as_posix()
    return "/output/patch_code_bundles" in p or p.endswith("output/patch_code_bundles")


def build_cfg(
    *,
    src: Path,
    out: Path,
    publish_mode: str,
    gh_owner: str | None,
    gh_repo: str | None,
    gh_branch: str,
    gh_base: str,
    gh_token: str | None,
    publish_codebase: bool,
    publish_analysis: bool,
    publish_handoff: bool,
    publish_transport: bool,
    local_publish_root: Path | None,
    clean_before_publish: bool,
) -> NS:
    out.mkdir(parents=True, exist_ok=True)

    # Files that MUST land under output\design_manifest
    out_bundle  = out / "design_manifest.jsonl"
    out_runspec = out / "superbundle.run.json"
    out_guide   = out / "assistant_handoff.v1.json"
    out_sums    = out / "design_manifest.SHA256SUMS"

    # Discovery / filters (global defaults)
    exclude_globs = [
        "**/secret_management/**",
        "**/publish.local.json",
        "**/.git/**",
        "**/node_modules/**",
        "**/dist/**",
        "**/build/**",
        # "**/output/**",   # <— normally exclude all output trees
        "**/software/**",
        "**/__pycache__/**",
        "**/*.pyc",
        # avoid packaging ourselves
        "**/backend/core/utils/code_bundles/code_bundles/**",
    ]
    segment_excludes = [
        ".git", ".hg", ".svn", "__pycache__", ".venv", "venv",
        "node_modules", "dist", "build", "software",
    ]
    # "output",

    # ---- EXCEPTION: allow ONLY the mirror subtree -------------------------
    # If src points at the mirror, drop 'output' from both filters,
    # keeping all other global excludes intact.
    if _under_allowed_mirror(src):
        exclude_globs = [g for g in exclude_globs if g != "**/output/**"]
        segment_excludes = [s for s in segment_excludes if s != "output"]

    # Transport: chunked parts under <out> with index & sums; no monolith kept
    transport = Transport(
        chunk_bytes=64000,
        chunk_records=True,
        group_dirs=True,
        dir_suffix_width=2,
        parts_per_dir=10,
        part_ext=".txt",
        part_stem="design_manifest",
        parts_index_name="design_manifest_parts_index.json",
        split_bytes=300000,
        transport_as_text=True,
        preserve_monolith=False,
    )

    # Publish config (GitHub target only when in github/both)
    gh = None
    if publish_mode in ("github", "both"):
        gh = GitHubTarget(owner=gh_owner or "", repo=gh_repo or "",
                          branch=gh_branch, base_path=gh_base)

    publish = PublishOptions(
        mode=publish_mode,
        publish_codebase=publish_codebase,
        publish_analysis=publish_analysis,
        publish_handoff=publish_handoff,
        publish_transport=publish_transport,
        github=gh,
        github_token=gh_token or "",
        local_publish_root=local_publish_root,
        clean_before_publish=clean_before_publish,
    )

    cfg = NS(
        # staging / discovery
        source_root=src,
        emitted_prefix="output/patch_code_bundles/",
        include_globs=[],
        exclude_globs=exclude_globs,
        follow_symlinks=False,
        case_insensitive=False,
        segment_excludes=segment_excludes,

        # outputs
        out_bundle=out_bundle,
        out_runspec=out_runspec,
        out_guide=out_guide,
        out_sums=out_sums,

        # features
        transport=transport,
        publish=publish,

        # prompts (unused; keep explicit)
        prompts=None,
        prompt_mode="none",
    )
    return cfg


def _is_excluded(rel: str, exclude_globs: list[str], segment_excludes: list[str]) -> bool:
    """Mirror packager filters for the URL printer."""
    # glob-based excludes
    for pat in exclude_globs or []:
        if fnmatch.fnmatch(rel, pat):
            # Exception: allow the mirror subtree even if '**/output/**' matches
            if rel.startswith("output/patch_code_bundles/"):
                continue
            return True
    # segment-based excludes
    parts = set(Path(rel).parts)
    segs = set(segment_excludes or [])
    # Exception for the mirror subtree: ignore 'output' segment
    if rel.startswith("output/patch_code_bundles/"):
        segs.discard("output")
    if any(seg in parts for seg in segs):
        return True
    return False


def gather_emitted_paths(
    src_root: Path,
    emitted_prefix: str,
    *,
    exclude_globs: list[str] | None = None,
    segment_excludes: list[str] | None = None,
) -> list[str]:
    """
    Return files under src_root, filtered like the packager, prefixed by emitted_prefix.
    Normalizes away a leading 'codebase/' if present in rel paths.
    """
    prefix = emitted_prefix if emitted_prefix.endswith("/") else (emitted_prefix + "/")
    out: list[str] = []
    for p in sorted(src_root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(src_root).as_posix()
        if rel.startswith("codebase/"):
            rel = rel[len("codebase/"):]
        if _is_excluded(rel, exclude_globs or [], segment_excludes or []):
            continue
        out.append(f"{prefix}{rel}")
    return out


def print_github_raw_urls(owner: str, repo: str, branch: str, base_path: str, paths: list[str]) -> None:
    """
    Print raw.githubusercontent.com URLs for each emitted path.
    Example base:
      https://raw.githubusercontent.com/{owner}/{repo}/refs/heads/{branch}/
    """
    base = f"https://raw.githubusercontent.com/{owner}/{repo}/refs/heads/{branch}/"
    prefix = (base_path.strip("/") + "/") if base_path else ""
    for p in paths:
        p_rel = p.lstrip("/")
        print(base + prefix + p_rel)


def main() -> int:
    p = argparse.ArgumentParser(description="Package a code tree and (optionally) publish.")
    p.add_argument("--src", type=Path, default=DEFAULT_SRC, help="Staging root (becomes output/patch_code_bundles/)")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output root for bundle/parts (output/design_manifest)")
    p.add_argument("--publish-mode", choices=["local", "github", "both"], default=None)
    p.add_argument("--gh-owner", type=str, default=None)
    p.add_argument("--gh-repo", type=str, default=None)
    p.add_argument("--gh-branch", type=str, default=None)
    p.add_argument("--gh-base", type=str, default=None)
    p.add_argument("--gh-token", type=str, default=None)
    # by default copy FROM your specified path into output/patch_code_bundles/
    p.add_argument("--ingest", type=Path, default=DEFAULT_INGEST, help="External source to copy into output/patch_code_bundles/")
    # optional overrides for publish flags/root
    p.add_argument("--local-publish-root", type=Path, default=None)
    p.add_argument("--publish-codebase", type=str, default=None)
    p.add_argument("--publish-analysis", type=str, default=None)
    p.add_argument("--publish-handoff", type=str, default=None)
    p.add_argument("--publish-transport", type=str, default=None)
    p.add_argument("--clean-before-publish", type=str, default=None)

    args = p.parse_args()

    # Load secrets (full structure supported)
    sec = _load_secrets(DEFAULT_SECRET)
    ghsec = sec.get("github") or {}

    # Effective values: CLI takes precedence; then secrets; then sensible defaults
    eff_mode   = args.publish_mode or sec.get("mode") or "github"

    eff_owner  = args.gh_owner  or ghsec.get("owner") or sec.get("owner")
    eff_repo   = args.gh_repo   or ghsec.get("repo")  or sec.get("repo")
    eff_branch = args.gh_branch or ghsec.get("branch") or sec.get("branch") or "main"
    eff_base   = args.gh_base   or ghsec.get("base_path") or sec.get("base_path") or sec.get("base") or ""
    eff_token  = args.gh_token  or sec.get("github_token") or sec.get("token") or (ghsec.get("token") if isinstance(ghsec, dict) else None)

    eff_local_root = args.local_publish_root or (Path(sec["local_publish_root"]) if sec.get("local_publish_root") else None)

    # DEFAULTS CHANGED: do NOT publish codebase unless explicitly enabled
    eff_pub_code      = _bool(args.publish_codebase,  default=_bool(sec.get("publish_codebase"), False))
    eff_pub_analysis  = _bool(args.publish_analysis,  default=_bool(sec.get("publish_analysis"), False))
    eff_pub_handoff   = _bool(args.publish_handoff,   default=_bool(sec.get("publish_handoff"), True))
    eff_pub_transport = _bool(args.publish_transport, default=_bool(sec.get("publish_transport"), False))
    eff_clean         = _bool(args.clean_before_publish, default=_bool(sec.get("clean_before_publish"), False))

    # Ensure staging / output exist
    args.src.mkdir(parents=True, exist_ok=True)
    args.out.mkdir(parents=True, exist_ok=True)

    # If the staging root itself contains a "codebase" folder, treat THAT as the real src.
    src_dir = args.src
    if (src_dir / "codebase").exists():
        src_dir = src_dir / "codebase"

    cfg = build_cfg(
        src=src_dir,
        out=args.out,
        publish_mode=eff_mode,
        gh_owner=eff_owner,
        gh_repo=eff_repo,
        gh_branch=eff_branch,
        gh_base=eff_base,
        gh_token=eff_token,
        publish_codebase=eff_pub_code,
        publish_analysis=eff_pub_analysis,
        publish_handoff=eff_pub_handoff,
        publish_transport=eff_pub_transport,
        local_publish_root=eff_local_root,
        clean_before_publish=eff_clean,
    )

    # --- Provenance + active filters (so you can SEE what's actually used) -----
    print(f"[packager] using orchestrator from: {inspect.getsourcefile(orch_mod) or '?'}")
    print(f"[packager] src_dir: {src_dir}")
    print(f"[packager] emitted_prefix: {cfg.emitted_prefix}")
    print(f"[packager] exclude_globs: {list(cfg.exclude_globs)}")
    print(f"[packager] segment_excludes: {list(cfg.segment_excludes)}")

    print("[packager] Packager: start")
    external = args.ingest if args.ingest else None
    if external and not external.exists():
        print(f"[packager] ERROR: --ingest path not found: {external}", file=sys.stderr)
        return 3

    result = Packager(cfg, rules=None).run(external_source=external)

    print(f"Bundle: {result.out_bundle}")
    print(f"Run-spec: {result.out_runspec}")
    print(f"Guide: {result.out_guide}")

    # --- Print GitHub raw URLs ONLY when we're actually publishing code --------
    if eff_mode in ("github", "both") and eff_pub_code:
        emitted_paths = gather_emitted_paths(
            src_dir,
            cfg.emitted_prefix,
            exclude_globs=list(cfg.exclude_globs),
            segment_excludes=list(cfg.segment_excludes),
        )
        print("[packager] GitHub Raw URLs:")
        print_github_raw_urls(eff_owner, eff_repo, eff_branch, eff_base, emitted_paths)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

