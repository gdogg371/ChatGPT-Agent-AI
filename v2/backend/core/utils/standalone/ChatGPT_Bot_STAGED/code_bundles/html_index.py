# File: v2/backend/core/utils/code_bundles/code_bundles/html_index.py
"""
HTML indexer (stdlib-only).

Emits JSONL-ready records:

  • html.file
      - Basic metadata for each HTML document:
        title, lang, doctype_html5, meta (description/keywords/viewport/robots),
        counts (headings, links, scripts, stylesheets, images, forms),
        link breakdown (internal/external), script breakdown (inline/external),
        canonical URL (if present), framework_hints, size, sha256.

  • html.summary
      - Aggregated counts and top pages (by link count, script count, image count).

Notes
-----
* Uses only Python's standard library (html.parser).
* Paths are repo-relative POSIX; the caller may remap them (local vs GitHub).
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

RepoItem = Tuple[Path, str]  # (local_path, repo_relative_posix)

_HTML_EXTS = {".html", ".htm", ".xhtml", ".shtml"}
_MAX_READ = 2 * 1024 * 1024  # 2 MiB cap per file

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _is_html_path(p: Path) -> bool:
    return p.suffix.lower() in _HTML_EXTS

def _read_text_limited(p: Path, limit: int = _MAX_READ) -> str:
    try:
        b = p.read_bytes()[: limit + 1]
        if len(b) > limit:
            b = b[:limit]
        return b.decode("utf-8", errors="replace")
    except Exception:
        return ""

def _sha256_file(p: Path, byte_limit: Optional[int] = None) -> Optional[str]:
    try:
        h = hashlib.sha256()
        if byte_limit is None:
            with p.open("rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
        else:
            h.update(p.read_bytes()[:byte_limit])
        return h.hexdigest()
    except Exception:
        return None

# ──────────────────────────────────────────────────────────────────────────────
# HTML parsing
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class HtmlDocInfo:
    title: Optional[str] = None
    lang: Optional[str] = None
    doctype_html5: bool = False
    meta: Dict[str, Optional[str]] = field(default_factory=lambda: {
        "description": None,
        "keywords": None,
        "viewport": None,
        "robots": None,
    })
    counts: Dict[str, int] = field(default_factory=lambda: {
        "h1": 0, "h2": 0, "h3": 0,
        "links": 0, "links_external": 0, "links_internal": 0,
        "scripts": 0, "scripts_inline": 0, "scripts_external": 0,
        "stylesheets": 0,
        "images": 0,
        "forms": 0,
    })
    canonical: Optional[str] = None
    framework_hints: List[str] = field(default_factory=list)

class _HTMLScanner(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.info = HtmlDocInfo()
        self._in_title = False
        self._seen_html_tag = False

    # DOCTYPE detection: html.parser feeds it via .handle_decl
    def handle_decl(self, decl: str) -> None:  # e.g., "DOCTYPE html"
        if decl.strip().lower().startswith("doctype html"):
            self.info.doctype_html5 = True

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        a = {k.lower(): (v or "") for k, v in attrs}
        if t == "html":
            self._seen_html_tag = True
            # lang attribute
            if "lang" in a and a["lang"].strip():
                self.info.lang = a["lang"].strip()
        elif t in ("h1", "h2", "h3"):
            self.info.counts[t] += 1
        elif t == "a":
            href = a.get("href", "").strip()
            if href:
                self.info.counts["links"] += 1
                if href.startswith(("http://", "https://", "//")):
                    self.info.counts["links_external"] += 1
                else:
                    self.info.counts["links_internal"] += 1
        elif t == "script":
            self.info.counts["scripts"] += 1
            src = a.get("src", "").strip()
            if src:
                self.info.counts["scripts_external"] += 1
                # quick framework hints from popular CDN/classic bundles
                low = src.lower()
                if "react" in low:
                    self._add_hint("react")
                if "vue" in low:
                    self._add_hint("vue")
                if "angular" in low:
                    self._add_hint("angular")
                if "svelte" in low:
                    self._add_hint("svelte")
            else:
                self.info.counts["scripts_inline"] += 1
        elif t == "link":
            rel = (a.get("rel") or "").lower()
            href = a.get("href", "")
            if "stylesheet" in rel:
                self.info.counts["stylesheets"] += 1
            if "canonical" in rel and href:
                self.info.canonical = href
        elif t == "img":
            self.info.counts["images"] += 1
        elif t == "form":
            self.info.counts["forms"] += 1
        elif t == "meta":
            name = (a.get("name") or a.get("property") or "").lower()
            content = a.get("content") or ""
            if name in ("description", "keywords", "viewport", "robots"):
                if not self.info.meta.get(name):
                    self.info.meta[name] = content
            # heuristics for frameworks via meta tags
            if "generator" == name:
                gen = content.lower()
                if "next.js" in gen:
                    self._add_hint("nextjs")
                if "gatsby" in gen:
                    self._add_hint("gatsby")
        elif t == "title":
            self._in_title = True
        # heuristic for SPA mount points
        if t == "div":
            idv = a.get("id", "").lower()
            cls = a.get("class", "").lower()
            if idv in ("root", "app", "react-root") or "root" in cls or "app" in cls:
                self._add_hint("spa_mount")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            # First non-empty chunk wins
            s = (data or "").strip()
            if s and not self.info.title:
                # truncate to a reasonable size
                self.info.title = s[:300]

    def _add_hint(self, hint: str) -> None:
        if hint not in self.info.framework_hints:
            self.info.framework_hints.append(hint)

# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def _analyze_html_text(text: str) -> HtmlDocInfo:
    parser = _HTMLScanner()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        # Best-effort: return whatever was collected
        pass
    # If no <!DOCTYPE html> but we saw <html>, guess HTML5 if nothing contradicts
    if not parser.info.doctype_html5 and parser._seen_html_tag:
        # Heuristic: presence of <meta charset> implies HTML5 often
        if '<meta charset=' in text.lower():
            parser.info.doctype_html5 = True
    return parser.info

def scan(repo_root: Path, discovered: Iterable[RepoItem]) -> List[Dict]:
    """
    Index HTML files from the discovered set and emit:
      • html.file (one per HTML document)
      • html.summary (one per run)
    """
    records: List[Dict] = []
    totals = Counter()
    top_by_links: List[Tuple[int, str]] = []
    top_by_scripts: List[Tuple[int, str]] = []
    top_by_images: List[Tuple[int, str]] = []

    for local, rel in discovered:
        if not _is_html_path(local):
            continue

        text = _read_text_limited(local)
        if not text:
            continue

        info = _analyze_html_text(text)

        try:
            size = int(local.stat().st_size)
        except Exception:
            size = len(text.encode("utf-8", errors="ignore"))
        sha = _sha256_file(local)

        rec = {
            "kind": "html.file",
            "path": rel,
            "size": size,
            "sha256": sha,
            "title": info.title,
            "lang": info.lang,
            "doctype_html5": bool(info.doctype_html5),
            "meta": info.meta,
            "counts": info.counts,
            "canonical": info.canonical,
            "framework_hints": info.framework_hints,
        }
        records.append(rec)

        # aggregates
        totals["files"] += 1
        for k, v in info.counts.items():
            totals[k] += int(v or 0)
        top_by_links.append((info.counts.get("links", 0), rel))
        top_by_scripts.append((info.counts.get("scripts", 0), rel))
        top_by_images.append((info.counts.get("images", 0), rel))

    def _top(lst: List[Tuple[int, str]], n: int = 10) -> List[Dict]:
        return [{"path": p, "count": c} for (c, p) in sorted(lst, key=lambda t: (-t[0], t[1]))[:n]]

    summary = {
        "kind": "html.summary",
        "files": int(totals.get("files", 0)),
        "totals": {
            "h1": int(totals.get("h1", 0)),
            "h2": int(totals.get("h2", 0)),
            "h3": int(totals.get("h3", 0)),
            "links": int(totals.get("links", 0)),
            "links_external": int(totals.get("links_external", 0)),
            "links_internal": int(totals.get("links_internal", 0)),
            "scripts": int(totals.get("scripts", 0)),
            "scripts_inline": int(totals.get("scripts_inline", 0)),
            "scripts_external": int(totals.get("scripts_external", 0)),
            "stylesheets": int(totals.get("stylesheets", 0)),
            "images": int(totals.get("images", 0)),
            "forms": int(totals.get("forms", 0)),
        },
        "top": {
            "by_links": _top(top_by_links),
            "by_scripts": _top(top_by_scripts),
            "by_images": _top(top_by_images),
        },
    }
    records.append(summary)
    return records


__all__ = ["scan"]
