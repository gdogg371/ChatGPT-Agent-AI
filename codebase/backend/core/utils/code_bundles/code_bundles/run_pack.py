# v2/backend/core/utils/code_bundles/code_bundles/run_pack.py
from __future__ import annotations

import argparse
import sys
import json
from pathlib import Path
from types import SimpleNamespace as NS

# Ensure we can import from ./src
ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# --- Robust import of Packager (absolute then relative) ------------------------
try:
    from packager.core.orchestrator import Packager  # type: ignore
except Exception:
    from src.packager.core.orchestrator import Packager  # type: ignore

# --- Hard-coded paths ----------------------------------------------------------
DEFAULT_SRC    = ROOT / "codebase"  # GitHub code is staged from here
DEFAULT_OUT    = Path(r"C:\Users\cg371\PycharmProjects\ChatGPT Bot\v2\patches\output\design_manifest")
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

    # Files that MUST land under patches\output\design_manifest
    out_bundle  = out / "design_manifest.jsonl"
    out_runspec = out / "superbundle.run.json"
    out_guide   = out / "assistant_handoff.v1.json"
    out_sums    = out / "design_manifest.SHA256SUMS"

    # Discovery / filters (aligned with your snapshot)
    exclude_globs = [
        "**/secret_management/**",
        "**/publish.local.json",
        "**/.git/**",
        "**/node_modules/**",
        "**/dist/**",
        "**/build/**",
        "**/output/**",
        "**/software/**",
        "**/__pycache__/**",
        "**/*.pyc",
        "**/backend/core/utils/code_bundles/code_bundles/**",
    ]
    segment_excludes = [
        ".git", ".hg", ".svn", "__pycache__", ".venv", "venv",
        "node_modules", "dist", "build", "output", "software",
    ]

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
        emitted_prefix="codebase/",
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


def main() -> int:
    p = argparse.ArgumentParser(description="Package a code tree and (optionally) publish.")
    p.add_argument("--src", type=Path, default=DEFAULT_SRC, help="Staging root (becomes codebase/)")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output root for bundle/parts")
    p.add_argument("--publish-mode", choices=["local", "github", "both"], default=None)
    p.add_argument("--gh-owner", type=str, default=None)
    p.add_argument("--gh-repo", type=str, default=None)
    p.add_argument("--gh-branch", type=str, default=None)
    p.add_argument("--gh-base", type=str, default=None)
    p.add_argument("--gh-token", type=str, default=None)
    # by default copy FROM your specified path into codebase/
    p.add_argument("--ingest", type=Path, default=DEFAULT_INGEST, help="External source to copy into codebase/")
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

    eff_pub_code     = _bool(args.publish_codebase, default=_bool(sec.get("publish_codebase"), True))
    eff_pub_analysis = _bool(args.publish_analysis, default=_bool(sec.get("publish_analysis"), False))
    eff_pub_handoff  = _bool(args.publish_handoff,  default=_bool(sec.get("publish_handoff"), True))
    eff_pub_transport= _bool(args.publish_transport,default=_bool(sec.get("publish_transport"), False))
    eff_clean        = _bool(args.clean_before_publish, default=_bool(sec.get("clean_before_publish"), False))

    # Validate GitHub args if needed
    if eff_mode in ("github", "both"):
        missing = [k for k, v in dict(owner=eff_owner, repo=eff_repo, token=eff_token).items() if not v]
        if missing:
            print(f"[packager] ERROR: Missing GitHub {'/'.join(missing)}; provide flags or set in secrets file.", file=sys.stderr)
            return 2

    # Ensure staging / output exist
    args.src.mkdir(parents=True, exist_ok=True)
    args.out.mkdir(parents=True, exist_ok=True)

    cfg = build_cfg(
        src=args.src,
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

    print("[packager] Packager: start")
    external = args.ingest if args.ingest else None
    if external and not external.exists():
        print(f"[packager] ERROR: --ingest path not found: {external}", file=sys.stderr)
        return 3

    result = Packager(cfg, rules=None).run(external_source=external)

    print(f"Bundle: {result.out_bundle}")
    print(f"Run-spec: {result.out_runspec}")
    print(f"Guide: {result.out_guide}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
