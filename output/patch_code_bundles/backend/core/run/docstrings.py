# File: v2/backend/core/run/docstrings.py
"""
Docstrings runner (platform-agnostic)

Invoke:
    python -m v2.backend.core.run.docstrings
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

from v2.backend.core.spine import Spine, to_dict


# --------------------------- path resolution ---------------------------

_THIS = Path(__file__).resolve()


def _resolve_spine_dir() -> Path:
    env_dir = os.getenv("SPINE_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()

    candidates = [
        _THIS.parents[1] / "spine",                          # v2/backend/core/spine
        _THIS.parents[2] / "spine",                          # v2/backend/spine (fallback)
        _THIS.parents[3] / "backend" / "core" / "spine",     # alt
    ]
    for c in candidates:
        if (c / "capabilities.yml").exists():
            return c
    return candidates[0]


_SPINE_DIR = _resolve_spine_dir()
SPINE_CAPS_PATH = _SPINE_DIR / "capabilities.yml"
SPINE_PIPELINE_PATH = _SPINE_DIR / "pipelines" / "default" / "patch_loop.yml"

print(f"[spine.paths] caps={SPINE_CAPS_PATH} pipeline={SPINE_PIPELINE_PATH}")


# --------------------------- env helpers -------------------------------

def _as_bool(x: Any, default: bool = False) -> bool:
    if x is None: return default
    if isinstance(x, bool): return x
    s = str(x).strip().lower()
    if s in {"1","true","yes","on"}: return True
    if s in {"0","false","no","off"}: return False
    return default


def _as_int(x: Any, default: int) -> int:
    try: return int(x)
    except Exception: return default


def _as_json_list(x: Any, default: List[Any] | None = None) -> List[Any]:
    if x is None or x == "": return default or []
    if isinstance(x, list): return x
    try: return json.loads(x)
    except Exception: return default or []


# --------------------------- variables for pipeline --------------------

def _collect_variables() -> Dict[str, Any]:
    vars: Dict[str, Any] = {
        "RUN_FETCH": _as_bool(os.getenv("RUN_FETCH"), True),
        "RUN_ENRICH": _as_bool(os.getenv("RUN_ENRICH"), True),
        "RUN_BUILD": _as_bool(os.getenv("RUN_BUILD"), True),
        "RUN_LLM": _as_bool(os.getenv("RUN_LLM"), True),
        "RUN_UNPACK": _as_bool(os.getenv("RUN_UNPACK"), True),
        "RUN_SANITIZE": _as_bool(os.getenv("RUN_SANITIZE"), True),
        "RUN_VERIFY": _as_bool(os.getenv("RUN_VERIFY"), True),
        "RUN_WRITE": _as_bool(os.getenv("RUN_WRITE"), False),
    }

    vars["PROJECT_ROOT"] = os.getenv("PROJECT_ROOT") or str(Path.cwd())
    vars["SCAN_ROOT"] = os.getenv("SCAN_ROOT") or vars["PROJECT_ROOT"]
    vars["EXCLUDES"]  = _as_json_list(os.getenv("EXCLUDES"), [])

    # Windows-safe; space not percent-encoded
    default_db = "sqlite:///C:/Users/cg371/PycharmProjects/ChatGPT Bot/databases/bot_dev.db"
    env_db = os.getenv("DB_URL")
    vars["DB_URL"] = env_db if (env_db or "").strip() else default_db
    vars["TABLE"]  = os.getenv("TABLE") or "introspection_index"
    vars["STATUS"] = os.getenv("STATUS") or "todo"
    vars["MAX_ROWS"] = _as_int(os.getenv("MAX_ROWS"), 200)

    vars["PROVIDER"] = os.getenv("PROVIDER") or "openai"
    vars["MODEL"]    = os.getenv("MODEL") or "gpt-4o-mini"
    vars["API_KEY"]  = os.getenv("API_KEY") or os.getenv("OPENAI_API_KEY") or ""

    vars["MODEL_CTX"]             = _as_int(os.getenv("MODEL_CTX"), 128000)
    vars["RESP_TOKENS_PER_ITEM"]  = _as_int(os.getenv("RESP_TOKENS_PER_ITEM"), 350)
    vars["BATCH_OVERHEAD_TOKENS"] = _as_int(os.getenv("BATCH_OVERHEAD_TOKENS"), 120)
    vars["BUDGET_GUARDRAIL"]      = float(os.getenv("BUDGET_GUARDRAIL") or 0.9)
    vars["BATCH_SIZE"]            = _as_int(os.getenv("BATCH_SIZE"), 20)

    vars["items"] = _as_json_list(os.getenv("ITEMS"), [])
    return vars


def _ensure_sqlite_dir(url: str) -> None:
    prefix = "sqlite:///"
    if not isinstance(url, str) or not url.startswith(prefix): return
    Path(url[len(prefix):]).parent.mkdir(parents=True, exist_ok=True)


# --------------------------- artifact printer -------------------------

def _summarize_result(uri: str, meta: Dict[str, Any]) -> str:
    res = meta.get("result")
    if res is None:
        return ""
    # write
    if uri.endswith("introspect.write.v1") or uri.endswith("introspec.write.v1"):
        w = (res or {}).get("written")
        return f"(written={w})" if w is not None else ""
    # fetch
    if uri.endswith("introspect.fetch.v1") or uri.endswith("introspec.fetch.v1"):
        rows = (res or {}).get("result") or []
        return f"(fetched={len(rows)})"
    # unpack
    if isinstance(res, dict) and "parsed" in res:
        parsed = res.get("parsed") or []
        return f"(parsed={len(parsed)})"
    # verify
    if isinstance(res, dict) and "reports" in res:
        reps = res["reports"] or []
        oks = sum(1 for r in reps if r.get("ok"))
        return f"(reports={len(reps)}, ok={oks})"
    # sanitize -> list of rows
    if isinstance(res, list):
        # docstrings.sanitize returns list of rows to write
        if res and isinstance(res[0], dict) and ("filepath" in res[0] or "description" in res[0]):
            return f"(sanitized={len(res)})"
        return f"(items={len(res)})"
    # llm batches
    if isinstance(res, dict) and "raw" in res:
        raw = res.get("raw") or []
        return f"(llm_responses={len(raw)})"
    # generic
    try:
        j = json.dumps(res)
        if len(j) > 80:
            j = j[:77] + "..."
        return f" {j}"
    except Exception:
        return ""


def _print_artifacts(arts: List[Any]) -> None:
    print("\n=== Spine Run: Artifacts ===")
    if not arts:
        print("(none)")
        return

    problems: List[Dict[str, Any]] = []
    for idx, a in enumerate(arts, 1):
        d = to_dict(a)
        kind = d.get("kind")
        uri  = d.get("uri")
        extra = ""
        if kind == "Result":
            extra = " " + _summarize_result(uri or "", d.get("meta") or {})
        print(f"{idx:02d}. {kind:7} {uri}{extra}")
        if kind == "Problem":
            pr = (d.get("meta") or {}).get("problem") or {}
            problems.append(pr)

    if problems:
        print("\nProblems:")
        for pr in problems:
            code = pr.get("code", "Unknown")
            msg  = pr.get("message", "")
            print(f" - {code}: {msg}")


# --------------------------- main -------------------------------------

def main() -> int:
    if not SPINE_CAPS_PATH.exists():
        print(f"ERROR: capabilities file not found: {SPINE_CAPS_PATH}", file=sys.stderr)
        print("[hint] Set SPINE_DIR to the folder containing capabilities.yml.", file=sys.stderr)
        return 2
    if not SPINE_PIPELINE_PATH.exists():
        print(f"ERROR: pipeline file not found: {SPINE_PIPELINE_PATH}", file=sys.stderr)
        return 2

    variables = _collect_variables()
    _ensure_sqlite_dir(variables.get("DB_URL", ""))

    # echo key vars so you can see what actually ran
    print("[vars] DB_URL=", variables["DB_URL"])
    print("[vars] TABLE =", variables["TABLE"])
    print("[vars] RUNS  =", {k:v for k,v in variables.items() if k.startswith("RUN_")})

    spine = Spine(caps_path=SPINE_CAPS_PATH)
    artifacts = spine.load_pipeline_and_run(SPINE_PIPELINE_PATH, variables=variables)

    _print_artifacts(artifacts)

    exit_code = 0
    for a in artifacts:
        if getattr(a, "kind", None) == "Problem" or to_dict(a).get("kind") == "Problem":
            exit_code = 1
            break
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

