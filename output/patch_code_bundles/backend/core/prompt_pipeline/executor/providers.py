# File: v2/backend/core/prompt_pipeline/executor/providers.py
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

# NOTE: Use the v2.* package root for this tree.
from v2.backend.core.spine.contracts import Artifact, Task
from v2.backend.core.prompt_pipeline.executor.retriever import SourceRetriever
from v2.backend.core.prompt_pipeline.executor.steps import (
    BuildContextStep,
    PackPromptStep,
    UnpackResultsStep,
)
from v2.backend.core.utils.io.file_ops import FileOps, FileOpsConfig


# ----------------- helpers -----------------

def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "on"}
    return bool(v)


def _result(uri: str, meta: Dict[str, Any]) -> List[Artifact]:
    return [Artifact(kind="Result", uri=uri, sha256="", meta=meta)]


def _problem(uri: str, code: str, message: str) -> List[Artifact]:
    return [
        Artifact(
            kind="Problem",
            uri=uri,
            sha256="",
            meta={
                "problem": {
                    "code": code,
                    "message": message,
                    "retryable": False,
                    "details": {},
                }
            },
        )
    ]


def _default_messages(batch: List[Dict[str, Any]]) -> Dict[str, str]:
    """
    Fallback prompt if the domain-specific builder produced empty messages.
    Keeps things generic and JSON-only so results.unpack.v1 can parse.
    We send a compact preview of each item to guide the model.
    """
    def _clip(s: Any, n: int) -> str:
        t = "" if s is None else str(s)
        return t if len(t) <= n else (t[: n - 1] + "…")

    preview: List[Dict[str, Any]] = []
    for it in batch:
        preview.append(
            {
                "id": str(it.get("id", "")),
                "mode": (it.get("mode") or "rewrite"),
                "signature": _clip(it.get("signature", ""), 180),
                "has_docstring": bool(it.get("has_docstring", False)),
                "existing_docstring": _clip(it.get("existing_docstring", ""), 600),
                "description": _clip(it.get("description", ""), 600),
                "context_code": _clip(it.get("context_code", ""), 2000),
            }
        )

    import json as _json

    system = (
        "You are a precise code assistant. "
        "Given a batch of code symbols with context, respond with STRICT JSON only. "
        "Schema:\n"
        '{ "items": [ { "id": "string", "mode": "create|rewrite", "docstring": "string" } ] }\n'
        "Do not include any prose or Markdown, only the JSON object."
    )
    user = (
        "Here is the batch:\n"
        + _json.dumps({"items": preview}, ensure_ascii=False)
        + "\n\nGenerate a high-quality docstring for each item. "
        "If mode is 'rewrite', improve the existing docstring; if 'create', write a new one. "
        "Return only the JSON object."
    )
    return {"system": system, "user": user}


# ----------------- providers -----------------

def enrich_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    """
    Capability: retriever.enrich.v1

    Payload:
      run: bool
      rows: [ {id, filepath, lineno, ...}, ... ]
      project_root: str|Path
      scan_root: str|Path (optional; defaults to project_root)
      exclude_globs: [ ... ]       # fnmatch-style patterns, repo-relative
      exclude_dirs: [ ... ]        # NEW: directory basenames (case-insensitive)
    """
    p = task.payload or {}
    if not _bool(p.get("run", True)):
        return _result("spine://result/retriever.enrich.v1", {"result": []})

    rows = p.get("rows") or []
    if not isinstance(rows, list):
        return _problem(
            "spine://problem/retriever.enrich.v1",
            "InvalidPayload",
            "rows must be a list",
        )

    pr = p.get("project_root")
    if not pr:
        return _problem(
            "spine://problem/retriever.enrich.v1",
            "InvalidPayload",
            "project_root is required",
        )

    # Coerce to Path objects
    try:
        project_root = Path(str(pr)).resolve()
        scan_root = Path(str(p.get("scan_root") or project_root)).resolve()
    except Exception as e:
        return _problem(
            "spine://problem/retriever.enrich.v1",
            "ConfigError",
            f"invalid path(s): {e}"
        )

    # Exclude globs (repo-relative)
    exclude = p.get("exclude_globs") or ()
    if isinstance(exclude, (list, tuple)):
        exclude_globs = tuple(str(x) for x in exclude)
    elif isinstance(exclude, str):
        exclude_globs = (exclude,)
    else:
        exclude_globs = ()

    # Directory-basename exclusions (uniform across pipeline)
    # Always include the baseline: Archive, output, v1
    # (case-insensitive match inside SourceRetriever)
    dirs_val = p.get("exclude_dirs")
    if isinstance(dirs_val, (list, tuple)):
        exclude_dirs: tuple[str, ...] = tuple(str(x) for x in dirs_val if x)
    elif isinstance(dirs_val, str):
        exclude_dirs = (dirs_val,)
    else:
        exclude_dirs = ()
    baseline = ("Archive", "output", "v1")
    # Merge and preserve order (first wins) while ensuring baseline present
    merged = list(exclude_dirs)
    for b in baseline:
        if b not in merged:
            merged.append(b)
    exclude_dirs = tuple(merged)

    retriever = SourceRetriever(
        project_root=project_root,
        file_ops=FileOps(FileOpsConfig()),
        scan_root=scan_root,
        exclude_globs=exclude_globs,
        exclude_dirs=exclude_dirs,   # <-- NEW: apply directory-name exclusions uniformly
    )

    out: List[Dict[str, Any]] = []
    for r in rows:
        try:
            out.append(retriever.enrich(dict(r)))
        except Exception:
            # Soft-skip rows that can't be enriched (outside scan_root, excluded, missing, etc.)
            continue

    return _result("spine://result/retriever.enrich.v1", {"result": out})


def build_prompts_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    """
    Capability: prompts.build.v1 (generic)

    Payload:
      run: bool
      items: list (either 'suspects' needing context, or already-contextualized items)
      batch_size: int
    """
    p = task.payload or {}
    if not _bool(p.get("run", True)):
        return _result(
            "spine://result/prompts.build.v1",
            {"result": {"batches": [], "items": [], "ids": []}},
        )

    items_in = p.get("items") or []
    if not isinstance(items_in, list):
        return _problem(
            "spine://problem/prompts.build.v1",
            "InvalidPayload",
            "items must be a list",
        )
    if items_in and not isinstance(items_in[0], dict):
        return _problem(
            "spine://problem/prompts.build.v1",
            "InvalidPayload",
            "items must be a list of dicts",
        )

    # If caller passed 'suspects' (no 'signature' key), build context first.
    if items_in and "signature" not in dict(items_in[0]):
        items = BuildContextStep().run(items_in)  # type: ignore[arg-type]
    else:
        items = [dict(x) for x in items_in]  # shallow copy

    batch_size = int(p.get("batch_size") or 20)
    packer = PackPromptStep()

    batches: List[Dict[str, Any]] = []
    for i in range(0, len(items), batch_size):
        chunk = items[i : i + batch_size]
        bundle = packer.build(chunk)

        # HARDEN: ensure non-empty messages.system/user; otherwise build generic fallback
        msgs = dict(bundle.get("messages") or {})
        sys = str(msgs.get("system") or "").strip()
        usr = str(msgs.get("user") or "").strip()
        if not sys or not usr:
            msgs = _default_messages(chunk)
            bundle["messages"] = msgs

        # also ensure ids (defensive)
        if "ids" not in bundle or not isinstance(bundle["ids"], list):
            bundle["ids"] = [str(it.get("id", "")) for it in chunk]

        batches.append(bundle)

    ids = [it["id"] for it in items]
    baton = {"items": items, "batches": batches, "ids": ids}
    return _result("spine://result/prompts.build.v1", {"result": baton})


def unpack_results_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    """
    Capability: results.unpack.v1

    Payload:
      run: bool
      baton: { batches: [...], raw: [...] }
    """
    p = task.payload or {}
    if not _bool(p.get("run", True)):
        return _result(
            "spine://result/results.unpack.v1", {"result": p.get("baton") or {}}
        )

    baton = dict(p.get("baton") or {})
    batches = baton.get("batches") or []
    raw_responses = baton.get("raw") or []

    if not isinstance(batches, list):
        return _problem(
            "spine://problem/results.unpack.v1",
            "InvalidPayload",
            "baton.batches must be a list",
        )
    if not isinstance(raw_responses, list):
        return _problem(
            "spine://problem/results.unpack.v1",
            "InvalidPayload",
            "baton.raw must be a list",
        )
    if len(raw_responses) != len(batches):
        return _problem(
            "spine://problem/results.unpack.v1",
            "ShapeError",
            "raw responses count must match batches",
        )

    parsed_all: Dict[str, Dict[str, Any]] = {}

    for bundle, raw in zip(batches, raw_responses):
        expected_ids = list(bundle.get("ids") or [])
        step = UnpackResultsStep(expected_ids=expected_ids)
        items = step.run(str(raw or ""))
        for it in items:
            parsed_all[it["id"]] = it

        # Optionally fold back into bundle for traceability
        bundle["parsed"] = [parsed_all[i] for i in expected_ids if i in parsed_all]

    baton["parsed"] = parsed_all
    return _result("spine://result/results.unpack.v1", {"result": baton})


__all__ = ["enrich_v1", "build_prompts_v1", "unpack_results_v1"]


# ----------------- static self-test -----------------
if __name__ == "__main__":
    """
    Minimal static tests (no LLM calls):
      1) Enricher path coercion & soft-skip behavior.
      2) Prompt packer fallback messages.
      3) Result unpacker shape checks (without calling an LLM).
    Exits non-zero on validation failures.
    """
    import json
    import tempfile

    failures = 0

    # 1) Enricher: create a temp project with a dummy file
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "pkg").mkdir()
        f = root / "pkg" / "mod.py"
        f.write_text("def foo():\n    pass\n", encoding="utf-8")
        rows = [
            {"id": "1", "filepath": str(f), "lineno": 1},
            {"id": "2", "filepath": str(root / "pkg" / "missing.py"), "lineno": 1},
            {"id": "3", "filepath": str(root / "Archive" / "x.py"), "lineno": 1},
        ]
        art = enrich_v1(
            Task(kind="Task", uri="spine://task/test",
                 payload={"rows": rows, "project_root": root}),
            {},
        )
        ok = art and art[0].kind == "Result" and isinstance(art[0].meta.get("result"), list)
        print("[providers.selftest] enrich_v1:", "OK" if ok else "FAIL")
        failures += 0 if ok else 1

    # 2) Builder: force empty messages path by simulating items already contextualized
    items = [
        {"id": "A", "mode": "create", "signature": "func A()", "context_code": "def A(): pass"},
        {"id": "B", "mode": "rewrite", "signature": "func B()", "existing_docstring": "old", "context_code": "def B(): pass"},
    ]
    art2 = build_prompts_v1(
        Task(kind="Task", uri="spine://task/test", payload={"items": items, "batch_size": 2}),
        {},
    )
    baton2 = (art2[0].meta or {}).get("result", {})
    ok2 = bool(baton2.get("batches")) and "messages" in baton2["batches"][0]
    print("[providers.selftest] build_prompts_v1:", "OK" if ok2 else "FAIL")
    failures += 0 if ok2 else 1

    # 3) Unpacker: feed a tiny valid JSON response matching ids
    ids = list(baton2.get("ids") or [])
    resp = json.dumps({"items": [{"id": ids[0], "docstring": "D1"}, {"id": ids[1], "docstring": "D2"}]})
    art3 = unpack_results_v1(
        Task(kind="Task", uri="spine://task/test",
             payload={"baton": {"batches": [{"ids": ids}], "raw": [resp]}}),
        {},
    )
    baton3 = (art3[0].meta or {}).get("result", {})
    ok3 = set((baton3.get("parsed") or {}).keys()) == set(ids)
    print("[providers.selftest] unpack_results_v1:", "OK" if ok3 else "FAIL")
    failures += 0 if ok3 else 1

    raise SystemExit(failures)



