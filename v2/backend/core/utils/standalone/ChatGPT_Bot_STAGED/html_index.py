# File: v2/backend/core/utils/code_bundles/code_bundles/html_index.py
"""
HTML indexer (stdlib-only).

Extracts lightweight metadata from .html/.htm files.

Per-file record (example)
-------------------------
{
  "kind": "html.index",
  "path": "web/index.html",
  "size": 12456,
  "lang": "en",
  "title": "Home",
  "meta": {
    "description": "…",
    "keywords": "…",
    "robots": "…",
    "charset": "utf-8",
    "canonical": "https://example.com/",
    "og": {"title":"…","description":"…","image":"…","type":"…","url":"…"},
    "twitter": {"card":"…","title":"…","description":"…","image":"…"}
  },
  "links": {"total": 42, "internal": 37, "external": 3, "mailto": 1, "tel": 1},
  "scripts": {"external": ["js/app.js", …], "inline_count": 3},
  "styles": {"external": ["css/app.css", …], "inline_count": 1},
  "images": {"count": 12, "missing_alt": 2},
  "headings": {
    "counts": {"h1": 1, "h2": 4, "h3": 7, "h4": 0, "h5": 0, "h6": 0},
    "h1": ["Main title"],
    "h2": ["Section A", "Section B"]
  }
}

Summary record
--------------
{
  "kind": "html.index.summary",
  "files": 17,
  "titles_missing": 2,
  "pages_with_canonical": 9,
  "pages_with_og": 11,
  "images_total": 210,
  "images_missing_alt": 13,
  "avg_links_per_page": 24.7,
  "top_langs": [{"lang":"en","count":14},{"lang":"fr","count":2},…]
}

Notes
-----
* Uses only the standard library (html.parser).
* Paths returned are repo-relative POSIX. If your pipeline distinguishes
  local vs GitHub path modes, map `path` before appending to the manifest.
* Lists of external scripts/styles are capped for safety.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

RepoItem = Tuple[Path, str]  # (local_path, repo_relative_posix)

_MAX_READ_BYTES = 2 * 1024 * 1024  # 2 MiB safety cap
_MAX_LIST = 100                    # cap lists of URLs we keep


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _read_text_limited(p: Path, limit: int = _MAX_READ_BYTES) -> str:
    try:
        with p.open("rb") as f:
            data = f.read(limit + 1)
        if len(data) > limit:
            data = data[:limit]
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""

def _is_external_href(href: str) -> bool:
    h = href.strip().lower()
    return h.startswith("http://") or h.startswith("https://")

def _is_mailto(href: str) -> bool:
    return href.strip().lower().startswith("mailto:")

def _is_tel(href: str) -> bool:
    return href.strip().lower().startswith("tel:")

def _trim(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    t = s.strip()
    return t if t else None

def _add_cap(lst: List[str], val: Optional[str], cap: int = _MAX_LIST) -> None:
    if val is None:
        return
    if len(lst) < cap:
        lst.append(val)


# ──────────────────────────────────────────────────────────────────────────────
# HTML parsing
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class _HTMLInfo:
    size: int = 0
    lang: Optional[str] = None
    title: Optional[str] = None
    meta: Dict[str, Optional[str]] = field(default_factory=lambda: {
        "description": None,
        "keywords": None,
        "robots": None,
        "charset": None,
        "canonical": None,
    })
    meta_og: Dict[str, str] = field(default_factory=dict)
    meta_twitter: Dict[str, str] = field(default_factory=dict)
    links_total: int = 0
    links_internal: int = 0
    links_external: int = 0
    links_mailto: int = 0
    links_tel: int = 0
    scripts_external: List[str] = field(default_factory=list)
    scripts_inline_count: int = 0
    styles_external: List[str] = field(default_factory=list)
    styles_inline_count: int = 0
    images_count: int = 0
    images_missing_alt: int = 0
    headings_counts: Dict[str, int] = field(default_factory=lambda: {f"h{i}": 0 for i in range(1, 7)})
    headings_text: Dict[str, List[str]] = field(default_factory=lambda: {f"h{i}": [] for i in range(1, 7)})

class _Indexer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.info = _HTMLInfo()
        self._in_title = False
        self._current_heading: Optional[str] = None
        self._heading_buffer: List[str] = []

    # --- tag starts
    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]):
        a = {k.lower(): (v if v is not None else "") for (k, v) in attrs}
        t = tag.lower()

        if t == "html":
            self.info.lang = _trim(a.get("lang") or self.info.lang)

        elif t == "title":
            self._in_title = True

        elif t == "meta":
            name = (a.get("name") or a.get("property") or "").strip().lower()
            content = _trim(a.get("content"))
            charset = _trim(a.get("charset"))
            if charset and not self.info.meta.get("charset"):
                self.info.meta["charset"] = charset
            if name == "description":
                self.info.meta["description"] = content or self.info.meta.get("description")
            elif name == "keywords":
                self.info.meta["keywords"] = content or self.info.meta.get("keywords")
            elif name == "robots":
                self.info.meta["robots"] = content or self.info.meta.get("robots")
            elif name.startswith("og:"):
                og_key = name.split(":", 1)[1]
                if content:
                    self.info.meta_og[og_key] = content
            elif name.startswith("twitter:"):
                tw_key = name.split(":", 1)[1]
                if content:
                    self.info.meta_twitter[tw_key] = content

        elif t == "link":
            rel = (a.get("rel") or "").lower()
            href = _trim(a.get("href"))
            if rel == "canonical" and href and not self.info.meta.get("canonical"):
                self.info.meta["canonical"] = href
            # Stylesheet?
            if "stylesheet" in rel and href:
                _add_cap(self.info.styles_external, href)

        elif t == "script":
            src = _trim(a.get("src"))
            if src:
                _add_cap(self.info.scripts_external, src)
            else:
                self.info.scripts_inline_count += 1

        elif t == "style":
            self.info.styles_inline_count += 1

        elif t == "a":
            href = _trim(a.get("href"))
            self.info.links_total += 1
            if not href:
                return
            if _is_mailto(href):
                self.info.links_mailto += 1
            elif _is_tel(href):
                self.info.links_tel += 1
            elif _is_external_href(href):
                self.info.links_external += 1
            else:
                self.info.links_internal += 1

        elif t == "img":
            self.info.images_count += 1
            alt = _trim(a.get("alt"))
            if not alt:
                self.info.images_missing_alt += 1

        # Headings
        if t in {f"h{i}" for i in range(1, 7)}:
            self._current_heading = t
            self._heading_buffer = []

    # --- text data
    def handle_data(self, data: str):
        if self._in_title:
            text = data.strip()
            if text:
                self.info.title = (self.info.title or "") + text
        if self._current_heading is not None:
            self._heading_buffer.append(data)

    # --- tag ends
    def handle_endtag(self, tag: str):
        t = tag.lower()
        if t == "title":
            self._in_title = False
            if self.info.title:
                self.info.title = self.info.title.strip()
        if self._current_heading == t:
            txt = " ".join(self._heading_buffer).strip()
            if txt:
                # Cap the list length for each heading level
                level_list = self.info.headings_text.get(t)
                if level_list is not None and len(level_list) < _MAX_LIST:
                    level_list.append(txt)
            self.info.headings_counts[t] = self.info.headings_counts.get(t, 0) + 1
            self._current_heading = None
            self._heading_buffer = []


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def analyze_file(*, local_path: Path, repo_rel_posix: str) -> Dict:
    """
    Analyze a single HTML file and return a JSON-ready dict.
    """
    text = _read_text_limited(local_path)
    parser = _Indexer()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        # best-effort; return what we have parsed so far
        pass

    info = parser.info
    try:
        size = local_path.stat().st_size
    except Exception:
        size = len(text.encode("utf-8", errors="ignore"))

    rec = {
        "kind": "html.index",
        "path": repo_rel_posix,
        "size": int(size),
        "lang": info.lang,
        "title": info.title,
        "meta": {
            "description": info.meta.get("description"),
            "keywords": info.meta.get("keywords"),
            "robots": info.meta.get("robots"),
            "charset": info.meta.get("charset"),
            "canonical": info.meta.get("canonical"),
            "og": dict(info.meta_og),
            "twitter": dict(info.meta_twitter),
        },
        "links": {
            "total": info.links_total,
            "internal": info.links_internal,
            "external": info.links_external,
            "mailto": info.links_mailto,
            "tel": info.links_tel,
        },
        "scripts": {
            "external": list(info.scripts_external),
            "inline_count": info.scripts_inline_count,
        },
        "styles": {
            "external": list(info.styles_external),
            "inline_count": info.styles_inline_count,
        },
        "images": {
            "count": info.images_count,
            "missing_alt": info.images_missing_alt,
        },
        "headings": {
            "counts": dict(info.headings_counts),
            "h1": list(info.headings_text.get("h1", [])),
            "h2": list(info.headings_text.get("h2", [])),
        },
    }
    return rec


def scan(repo_root: Path, discovered: Iterable[RepoItem]) -> List[Dict]:
    """
    Scan discovered files, indexing .html/.htm files and returning:
      - One 'html.index' record per HTML file
      - One 'html.index.summary' record at the end
    """
    items = [(lp, rel) for (lp, rel) in discovered if rel.lower().endswith((".html", ".htm"))]
    results: List[Dict] = []

    titles_missing = 0
    pages_with_canonical = 0
    pages_with_og = 0
    images_total = 0
    images_missing_alt = 0
    links_total = 0
    lang_counter: Counter[str] = Counter()

    for local, rel in items:
        rec = analyze_file(local_path=local, repo_rel_posix=rel)
        results.append(rec)

        if not (rec.get("title") or "").strip():
            titles_missing += 1
        meta = rec.get("meta") or {}
        if (meta.get("canonical") or "").strip():
            pages_with_canonical += 1
        og = meta.get("og") or {}
        if any(v for v in og.values()):
            pages_with_og += 1

        imgs = rec.get("images") or {}
        images_total += int(imgs.get("count", 0))
        images_missing_alt += int(imgs.get("missing_alt", 0))

        lnks = rec.get("links") or {}
        links_total += int(lnks.get("total", 0))

        lang = rec.get("lang")
        if isinstance(lang, str) and lang.strip():
            lang_counter[lang.strip().lower()] += 1

    avg_links = (links_total / float(len(items))) if items else 0.0
    top_langs = [{"lang": lang, "count": count} for (lang, count) in lang_counter.most_common(10)]

    summary = {
        "kind": "html.index.summary",
        "files": len(items),
        "titles_missing": titles_missing,
        "pages_with_canonical": pages_with_canonical,
        "pages_with_og": pages_with_og,
        "images_total": images_total,
        "images_missing_alt": images_missing_alt,
        "avg_links_per_page": round(avg_links, 3) if items else None,
        "top_langs": top_langs,
    }
    results.append(summary)
    return results


__all__ = ["scan", "analyze_file"]
