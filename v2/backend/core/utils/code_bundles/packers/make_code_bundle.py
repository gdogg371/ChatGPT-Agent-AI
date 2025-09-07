from __future__ import annotations
import base64, hashlib, json, sys
from pathlib import Path

#ROOT = Path(__file__).resolve().parents[1] / "tests_adhoc" / "patch_loop_test2"   # adjust if needed
#OUT  = Path(__file__).resolve().parents[0] / "output" / "code_bundles"/ "code_bundle.jsonl"
#SUMS = Path(__file__).resolve().parents[0] / "output" / "code_bundles"/ "code_bundle.SHA256SUMS"

ROOT = Path("C:\\Users\\cg371\\PycharmProjects\\ChatGPT Bot\\v2\\backend\\core\\utils\\code_bundles\\code_bundles\\")   # adjust if needed
OUT  = Path("/output/output_code_bundles\\code_bundle.jsonl")
SUMS = Path("/output/output_code_bundles\\code_bundle.SHA256SUMS")

EXCLUDE_DIRS = {".git", "__pycache__", ".mypy_cache", ".pytest_cache", "output", "dist", "build", ".venv", "venv"}
EXCLUDE_EXTS = {".pyc", ".pyo", ".pyd", ".log"}

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def main():
    root = ROOT
    files = []
    for p in root.rglob("*"):
        if p.is_dir():
            if p.name in EXCLUDE_DIRS:
                continue
            # skip excluded ancestors
            if any(part in EXCLUDE_DIRS for part in p.relative_to(root).parts):
                continue
            continue
        if p.suffix.lower() in EXCLUDE_EXTS:
            continue
        rel = p.relative_to(root).as_posix()
        b = p.read_bytes()
        record = {
            "path": rel,
            "sha256": sha256_bytes(b),
            "mode": "text",
            "content_b64": base64.b64encode(b).decode("ascii"),
        }
        files.append(record)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="\n") as f:
        for rec in files:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    with SUMS.open("w", encoding="utf-8", newline="\n") as f:
        for rec in files:
            f.write(f"{rec['sha256']}  {rec['path']}\n")

    print(f"[ok] wrote {len(files)} files → {OUT}")
    print(f"[ok] checksums → {SUMS}")

if __name__ == "__main__":
    main()
