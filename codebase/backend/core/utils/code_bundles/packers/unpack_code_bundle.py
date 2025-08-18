#!/usr/bin/env python3
from __future__ import annotations
import base64, hashlib, json
from pathlib import Path

# -------- hardcoded config (yours) --------
INPUT_BUNDLE = r"C:\Users\cg371\PycharmProjects\ChatGPT Bot\tests_adhoc\output\downloaded_code_bundles\class_based_source_bundle_20250816_222801.jsonl"
DEST_ROOT   = Path(r"C:\Users\cg371\PycharmProjects\ChatGPT Bot\tests_adhoc\output\downloaded_code_bundles").resolve()
STRICT = False
# -----------------------------------------

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def ensure_relative_safe(rel: str) -> Path:
    p = Path(rel.replace("\\", "/"))
    if p.is_absolute(): raise ValueError(f"absolute path not allowed: {rel}")
    if any(part == ".." for part in p.parts): raise ValueError(f"parent traversal not allowed: {rel}")
    if p.parts and p.parts[0].endswith(":"): raise ValueError(f"drive-qualified path not allowed: {rel}")
    return p

def detect_bundle_type(path: Path) -> str:
    head = path.read_bytes()[:512]
    if head.startswith(b"PK\x03\x04"): return "zip"
    txt = head.decode("utf-8", errors="ignore").lstrip("\ufeff").lstrip()
    if txt.startswith("{"): return "jsonl"
    # 64 hex + two spaces + path → sums
    import re
    if re.match(r"^[0-9a-fA-F]{64}\s\s\S", txt): return "sha256sums"
    return "unknown"

def iter_json_objects(raw: str):
    """Yield JSON object strings by scanning for balanced braces, ignoring braces in strings."""
    i, n = 0, len(raw)
    depth = 0
    start = -1
    in_str = False
    esc = False
    while i < n:
        ch = raw[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == '{':
                if depth == 0:
                    start = i
                depth += 1
            elif ch == '}':
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start != -1:
                        yield raw[start:i+1]
                        start = -1
        i += 1

def unpack(bundle: Path, strict: bool = False) -> tuple[int, int, dict]:
    kind = detect_bundle_type(bundle)
    if kind != "jsonl":
        raise SystemExit(
            f"[fatal] expected a JSONL bundle but detected '{kind}'. "
            "Use the *code_bundle.jsonl*, not the .zip or .SHA256SUMS."
        )

    data = bundle.read_bytes()
    print(f"[info] file size: {len(data)} bytes")
    print(f"[info] head(120): {data[:120]!r}")

    text = data.decode("utf-8", errors="ignore")
    if text and text[0] == "\ufeff":  # strip BOM
        text = text.lstrip("\ufeff")

    DEST_ROOT.mkdir(parents=True, exist_ok=True)
    dirs_created = 0
    files_written = 0
    stats = {"meta": 0, "dir": 0, "file": 0, "skipped": 0}

    found_any = False
    for obj_str in iter_json_objects(text):
        found_any = True
        try:
            rec = json.loads(obj_str)
        except Exception:
            stats["skipped"] += 1
            continue

        rtype = rec.get("type")
        if rtype == "meta":
            stats["meta"] += 1
            continue

        if rtype == "dir":
            try:
                rel = ensure_relative_safe(rec["path"].rstrip("/"))
                out_dir = (DEST_ROOT / rel).resolve()
                if DEST_ROOT not in out_dir.parents and out_dir != DEST_ROOT:
                    raise ValueError("unsafe dir path escapes dest")
                out_dir.mkdir(parents=True, exist_ok=True)
                dirs_created += 1
                stats["dir"] += 1
            except Exception as e:
                print(f"[warn] dir '{rec.get('path')}': {e}")
            continue

        if rtype == "file":
            try:
                rel = ensure_relative_safe(rec["path"])
                out_path = (DEST_ROOT / rel).resolve()
                if DEST_ROOT not in out_path.parents:
                    raise ValueError("unsafe file path escapes dest")
                b64 = rec.get("content_b64")
                if b64 is None:
                    raise ValueError("missing content_b64")
                blob = base64.b64decode(b64)
                want_sha = rec.get("sha256") or ""
                got_sha = sha256_bytes(blob)
                if want_sha and want_sha != got_sha:
                    msg = f"[warn] checksum mismatch for {rel}: want {want_sha}, got {got_sha}"
                    if strict: raise ValueError("checksum mismatch (strict mode)")
                    print(msg + " — writing anyway")
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(blob)
                files_written += 1
                stats["file"] += 1
                if files_written <= 3:
                    print(f"[write] {out_path}")
            except Exception as e:
                print(f"[warn] file '{rec.get('path')}': {e}")
            continue

        stats["skipped"] += 1

    if not found_any:
        print("[warn] no JSON objects found. This usually means the input is not the code_bundle.jsonl.")

    return dirs_created, files_written, stats

def main() -> int:
    bundle = Path(INPUT_BUNDLE)
    if not bundle.is_file():
        raise SystemExit(f"[fatal] bundle not found: {bundle}")
    print(f"[info] reading bundle: {bundle}")
    print(f"[info] writing under: {DEST_ROOT}")
    d, f, stats = unpack(bundle, strict=STRICT)
    print(f"[done] created {d} dirs, wrote {f} files")
    print(f"[stats] {stats}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())


