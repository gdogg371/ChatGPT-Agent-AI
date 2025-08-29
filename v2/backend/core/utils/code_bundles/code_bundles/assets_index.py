# File: v2/backend/core/utils/code_bundles/code_bundles/assets_index.py
"""
Assets indexer (stdlib-only).

Purpose
-------
Catalog non-code assets in the repository and emit JSONL-ready records:

  • asset.file
      - one record per asset with size, sha256, mime, category, ext
      - image metadata when available (width/height, format)
      - optional lightweight text stats for docs (line counts)

  • asset.summary
      - counts and total bytes per category
      - top largest files
      - image format breakdown and common dimension buckets

Notes
-----
* Uses only Python's standard library.
* Paths in records are repo-relative POSIX (map externally if you need a
  different path mode for "local" vs "github").
* We intentionally SKIP most source-code files; the goal is to describe
  static assets (images, media, fonts, archives, docs, binaries, …).
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import struct
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

RepoItem = Tuple[Path, str]  # (local_path, repo_relative_posix)

_MAX_BYTES_SHA256 = None      # None = full file; set an int to cap if desired
_MAX_TEXT_BYTES = 2 * 1024 * 1024  # 2 MiB cap when reading text-ish files
_TOP_N_LARGEST = 20

# Heuristic sets
CODE_EXTS = {
    # Python
    ".py", ".pyi", ".pyw",
    # Web
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".css", ".scss", ".sass", ".less",
    ".html", ".htm",
    # C/C++
    ".c", ".cc", ".cpp", ".h", ".hh", ".hpp",
    # Java / JVM
    ".java", ".kt", ".kts", ".scala", ".groovy",
    # .NET
    ".cs", ".vb", ".fs",
    # Rust / Go
    ".rs", ".go",
    # Shell / config-like code
    ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd", ".make", ".mk",
    # Templates
    ".jinja", ".j2", ".ejs", ".mustache", ".hbs",
}

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".tif", ".tiff", ".svg", ".webp"}
FONT_EXTS = {".woff", ".woff2", ".ttf", ".otf", ".eot"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".aiff"}
VIDEO_EXTS = {".mp4", ".m4v", ".webm", ".mov", ".avi", ".mkv"}
ARCHIVE_EXTS = {".zip", ".tar", ".gz", ".tgz", ".xz", ".7z", ".rar", ".bz2", ".jar", ".war"}
DOC_EXTS = {
    ".pdf", ".md", ".rst", ".txt", ".rtf",
    ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".csv", ".tsv", ".ipynb",
    ".json", ".jsonl", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".toml",
}
BINARY_EXTS = {".exe", ".dll", ".so", ".dylib", ".bin", ".o", ".a", ".class"}

TEXT_LIKE_EXTS = DOC_EXTS | {".svg", ".env"}

# ──────────────────────────────────────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────────────────────────────────────

def _sha256_file(path: Path, byte_limit: Optional[int] = _MAX_BYTES_SHA256) -> Optional[str]:
    try:
        h = hashlib.sha256()
        if byte_limit is None:
            with path.open("rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
        else:
            h.update(path.read_bytes()[:byte_limit])
        return h.hexdigest()
    except Exception:
        return None

def _guess_mime(rel_path: str) -> Optional[str]:
    m, _ = mimetypes.guess_type(rel_path)
    return m

def _is_text_ext(ext: str) -> bool:
    return ext.lower() in TEXT_LIKE_EXTS

def _safe_read_text(path: Path, limit: int = _MAX_TEXT_BYTES) -> str:
    try:
        data = path.read_bytes()[: limit + 1]
        if len(data) > limit:
            data = data[:limit]
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""

def _count_lines(text: str) -> int:
    if not text:
        return 0
    # normalize possible missing trailing newline
    return text.count("\n") + (0 if text.endswith("\n") else 1 if text else 0)

# ──────────────────────────────────────────────────────────────────────────────
# Image dimension sniffers (PNG, JPEG, GIF, WebP, BMP, SVG)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ImageInfo:
    width: Optional[int]
    height: Optional[int]
    format: Optional[str]

def _read_head(path: Path, n: int = 64 * 1024) -> bytes:
    try:
        with path.open("rb") as f:
            return f.read(n)
    except Exception:
        return b""

def _png_size(b: bytes) -> Optional[Tuple[int, int]]:
    if len(b) >= 24 and b[:8] == b"\x89PNG\r\n\x1a\n" and b[12:16] == b"IHDR":
        w, h = struct.unpack(">II", b[16:24])
        return int(w), int(h)
    return None

def _gif_size(b: bytes) -> Optional[Tuple[int, int]]:
    if len(b) >= 10 and (b[:6] in (b"GIF87a", b"GIF89a")):
        w, h = struct.unpack("<HH", b[6:10])
        return int(w), int(h)
    return None

def _bmp_size(b: bytes) -> Optional[Tuple[int, int]]:
    # BMP header: 'BM' + 12 bytes, then DIB header; most common: BITMAPINFOHEADER at offset 14
    if len(b) >= 26 and b[:2] == b"BM":
        w, h = struct.unpack("<ii", b[18:26])
        return abs(int(w)), abs(int(h))
    return None

def _jpeg_size(b: bytes) -> Optional[Tuple[int, int]]:
    # Walk JPEG markers to SOFn and read dimensions
    try:
        idx = 0
        if b[0:2] != b"\xFF\xD8":
            return None
        idx = 2
        n = len(b)
        while idx < n:
            if b[idx] != 0xFF:
                idx += 1
                continue
            # skip fill bytes 0xFF
            while idx < n and b[idx] == 0xFF:
                idx += 1
            if idx >= n:
                break
            marker = b[idx]
            idx += 1
            # Standalone markers
            if marker in (0x01, 0xD0,0xD1,0xD2,0xD3,0xD4,0xD5,0xD6,0xD7):
                continue
            if idx + 1 >= n:
                break
            seg_len = struct.unpack(">H", b[idx:idx+2])[0]
            if seg_len < 2:
                return None
            if marker in (0xC0,0xC1,0xC2,0xC3,0xC5,0xC6,0xC7,0xC9,0xCA,0xCB,0xCD,0xCE,0xCF):
                # SOFn: [len][precision][height][width]...
                if idx + 7 < n:
                    h, w = struct.unpack(">xHH", b[idx+2:idx+7+1])
                    return int(w), int(h)
                return None
            idx += seg_len
        return None
    except Exception:
        return None

def _webp_size(b: bytes) -> Optional[Tuple[int, int]]:
    # RIFF....WEBP....(VP8X|VP8 |VP8L)
    try:
        if len(b) < 16 or b[:4] != b"RIFF" or b[8:12] != b"WEBP":
            return None
        fourcc = b[12:16]
        if fourcc == b"VP8X" and len(b) >= 30:
            # VP8X: 10 bytes of header after chunk header; canvas size at bytes 24..29 as 24-bit little-endian minus one
            w_minus1 = int.from_bytes(b[24:27], "little")
            h_minus1 = int.from_bytes(b[27:30], "little")
            return w_minus1 + 1, h_minus1 + 1
        # Fallbacks not guaranteed without full parse; return None if unknown
        return None
    except Exception:
        return None

def _svg_size(text: str) -> Optional[Tuple[int, int]]:
    # Try width/height attributes first; fallback to viewBox
    try:
        w_m = re.search(r'(?i)\bwidth\s*=\s*"([^"]+)"', text)
        h_m = re.search(r'(?i)\bheight\s*=\s*"([^"]+)"', text)
        def _num(s: str) -> Optional[float]:
            m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)", s or "")
            return float(m.group(1)) if m else None
        if w_m and h_m:
            w = _num(w_m.group(1))
            h = _num(h_m.group(1))
            if w and h:
                return int(round(w)), int(round(h))
        vb = re.search(r'(?i)\bviewBox\s*=\s*"([^"]+)"', text)
        if vb:
            parts = [p for p in re.split(r"[ ,]+", vb.group(1).strip()) if p]
            if len(parts) == 4:
                w = float(parts[2]); h = float(parts[3])
                return int(round(w)), int(round(h))
    except Exception:
        pass
    return None

def sniff_image_info(path: Path) -> Optional[ImageInfo]:
    ext = path.suffix.lower()
    if ext == ".svg":
        text = _safe_read_text(path, limit=_MAX_TEXT_BYTES)
        wh = _svg_size(text)
        return ImageInfo(width=wh[0] if wh else None, height=wh[1] if wh else None, format="SVG")
    head = _read_head(path)
    for fn, fmt in ((_png_size, "PNG"), (_gif_size, "GIF"), (_jpeg_size, "JPEG"), (_webp_size, "WEBP"), (_bmp_size, "BMP")):
        wh = fn(head)
        if wh:
            return ImageInfo(width=wh[0], height=wh[1], format=fmt)
    return None

# ──────────────────────────────────────────────────────────────────────────────
# Categorization
# ──────────────────────────────────────────────────────────────────────────────

def categorize(rel_path: str) -> str:
    ext = Path(rel_path).suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in FONT_EXTS:
        return "font"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in ARCHIVE_EXTS:
        return "archive"
    if ext in DOC_EXTS:
        return "document"
    if ext in BINARY_EXTS:
        return "binary"
    if ext in CODE_EXTS:
        return "code"  # will be skipped by scanner
    # Generic mime-based fallback
    mime = _guess_mime(rel_path) or ""
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    if mime in ("application/pdf", "text/plain", "text/markdown", "application/json", "text/csv"):
        return "document"
    return "other"

# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def scan(repo_root: Path, discovered: Iterable[RepoItem]) -> List[Dict]:
    """
    Scan discovered files and emit:
      • asset.file for non-code assets
      • asset.summary (one per run)
    """
    records: List[Dict] = []
    cat_counter: Counter[str] = Counter()
    img_fmt_counter: Counter[str] = Counter()
    total_bytes_by_cat: Counter[str] = Counter()
    largest: List[Tuple[int, str]] = []  # (size, path)
    img_dims_buckets: Counter[str] = Counter()

    for local, rel in discovered:
        ext = Path(rel).suffix.lower()
        cat = categorize(rel)

        # Skip source code — other indexers handle that
        if cat == "code":
            continue

        try:
            size = int(local.stat().st_size)
        except Exception:
            size = 0

        sha = _sha256_file(local, byte_limit=_MAX_BYTES_SHA256)
        mime = _guess_mime(rel)

        rec: Dict = {
            "kind": "asset.file",
            "path": rel,
            "size": size,
            "sha256": sha,
            "ext": ext or None,
            "mime": mime,
            "category": cat,
        }

        # Image metadata
        if cat == "image":
            info = sniff_image_info(local)
            if info:
                rec["image"] = {
                    "width": info.width,
                    "height": info.height,
                    "format": info.format,
                }
                if info.format:
                    img_fmt_counter[info.format] += 1
                if info.width and info.height:
                    # bucket by megapixels-ish & orientation
                    mp = (info.width * info.height) / 1_000_000.0
                    if mp < 0.3:
                        b = "<0.3MP"
                    elif mp < 1:
                        b = "0.3–1MP"
                    elif mp < 3:
                        b = "1–3MP"
                    elif mp < 8:
                        b = "3–8MP"
                    else:
                        b = ">=8MP"
                    img_dims_buckets[b] += 1

        # Lightweight text stats for docs (line counts)
        if cat == "document" or (ext == ".svg"):
            text = _safe_read_text(local, limit=_MAX_TEXT_BYTES)
            if text:
                rec["text"] = {"lines": _count_lines(text)}

        records.append(rec)

        # Aggregates
        cat_counter[cat] += 1
        total_bytes_by_cat[cat] += size
        largest.append((size, rel))

    # Top-N largest
    largest_sorted = [{"path": p, "size": s} for (s, p) in sorted(largest, key=lambda t: (-t[0], t[1]))[:_TOP_N_LARGEST]]

    summary = {
        "kind": "asset.summary",
        "files": int(sum(cat_counter.values())),
        "by_category": dict(sorted(cat_counter.items(), key=lambda kv: (-kv[1], kv[0]))),
        "bytes_by_category": dict(sorted(total_bytes_by_cat.items(), key=lambda kv: (-kv[1], kv[0]))),
        "image_formats": dict(sorted(img_fmt_counter.items(), key=lambda kv: (-kv[1], kv[0]))),
        "image_dimension_buckets": dict(sorted(img_dims_buckets.items(), key=lambda kv: (-kv[1], kv[0]))),
        "largest_files": largest_sorted,
    }
    records.append(summary)
    return records


__all__ = ["scan", "categorize", "sniff_image_info"]
