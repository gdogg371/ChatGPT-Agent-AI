# File: backend/core/utils/code_bundles/code_bundles_v2/src/packager/languages/html/plugin.py
from __future__ import annotations

"""
HTML language plugin

Artifacts:
  analysis/html_assets.json  -- lists of script/src, link/href, img/src per file
"""

from typing import Any, Dict, List, Tuple
from html.parser import HTMLParser

PLUGIN_NAME = "html"
EXTENSIONS = (".html", ".htm")


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts: List[str] = []
        self.links: List[str] = []
        self.images: List[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script":
            src = attrs.get("src")
            if src:
                self.scripts.append(src)
        elif tag == "link":
            href = attrs.get("href")
            if href:
                self.links.append(href)
        elif tag == "img":
            src = attrs.get("src")
            if src:
                self.images.append(src)


class _HtmlPlugin:
    name = PLUGIN_NAME
    extensions = EXTENSIONS

    def analyze(self, files: List[Tuple[str, bytes]]) -> Dict[str, Any]:
        assets = []
        for path, data in files:
            try:
                text = data.decode("utf-8")
            except Exception as e:
                assets.append({"path": path, "error": f"decode: {type(e).__name__}: {e}"})
                continue
            p = _Collector()
            try:
                p.feed(text)
            except Exception as e:
                assets.append({"path": path, "error": f"parse: {type(e).__name__}: {e}"})
                continue
            assets.append({
                "path": path,
                "scripts": p.scripts,
                "links": p.links,
                "images": p.images,
            })
        return {
            "analysis/html_assets.json": {"version": "1", "files": assets}
        }

PLUGIN = _HtmlPlugin()
