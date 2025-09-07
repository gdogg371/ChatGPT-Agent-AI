# src/packager/patch_pack.py
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Dict, List, Iterable

from bundle_io import FileRec

__all__ = ["PatchPack"]


@dataclass(frozen=True)
class _Chunk:
    file_path: str
    base_sha: str
    byte_start: int
    byte_end: int
    start_line: int
    end_line: int
    sha256_chunk: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "file_path": self.file_path,
            "base_sha": self.base_sha,
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "sha256_chunk": self.sha256_chunk,
        }


class PatchPack:
    """
    Builders for patch/embedding artifacts used in the packaging pipeline.

    Notes
    -----
    - `empty_patch_pack(...)` mirrors your current behavior: creates a no-op
      unified diff per file (useful as a placeholder for agent workflows).
    - `build_rag_chunks(...)` produces byte-range chunks with best-effort
      UTF-8 line mapping (errors ignored, identical to your current logic).
    - `build_embedding_index(...)` is a deterministic hash-to-vector shim
      (keeps your existing “hash-{dim}” model behavior).
    """

    # ---------------- public API ----------------

    @staticmethod
    def empty_patch_pack(files: List[FileRec]) -> Dict[str, Any]:
        """
        Create a no-op patch pack for a set of files.

        Returns
        -------
        {"patches": [{"target_path","base_sha","new_sha","format","diff_text"}...]}
        """
        patches: List[Dict[str, Any]] = []
        for fr in files:
            patches.append({
                "target_path": fr.path,
                "base_sha": fr.sha256,
                "new_sha": fr.sha256,
                "format": "unified",
                "diff_text": "",
            })
        return {"patches": patches}

    @staticmethod
    def build_rag_chunks(files: List[FileRec], *, chunk_bytes: int = 2048) -> Dict[str, Any]:
        """
        Slice UTF-8 text files into fixed-size byte chunks with line metadata.

        Behavior matches the original:
        - Non-UTF-8 files are skipped.
        - Line numbers computed by decoding up to positions with errors="ignore".

        Returns
        -------
        {"chunks": [ {file_path, base_sha, byte_start, byte_end, start_line, end_line, sha256_chunk} ... ]}
        """
        chunks: List[_Chunk] = []
        for fr in files:
            # Skip non-text (same as your try/except sentinel)
            try:
                _ = fr.data.decode("utf-8")
            except Exception:
                continue

            b = fr.data
            i = 0
            n = len(b)
            while i < n:
                j = min(i + chunk_bytes, n)
                payload = b[i:j]
                sha = hashlib.sha256(payload).hexdigest()

                # Best-effort line mapping (keeps original behavior)
                sub = b[:j].decode("utf-8", errors="ignore")
                end_line = sub.count("\n") + 1
                sub2 = b[:i].decode("utf-8", errors="ignore")
                start_line = sub2.count("\n") + 1

                chunks.append(_Chunk(
                    file_path=fr.path,
                    base_sha=fr.sha256,
                    byte_start=i,
                    byte_end=j,
                    start_line=start_line,
                    end_line=end_line,
                    sha256_chunk=sha,
                ))
                i = j

        return {"chunks": [c.as_dict() for c in chunks]}

    @staticmethod
    def build_embedding_index(chunks: Dict[str, Any], *, dim: int = 64) -> Dict[str, Any]:
        """
        Deterministic “hash embedding” for each chunk (compat with current behavior).

        Returns
        -------
        {"model": "hash-{dim}", "dim": dim,
         "vectors": [{"chunk_id": "<path>:start-end", "embedding": [float,...]}]}
        """
        vectors: List[Dict[str, Any]] = []
        for ch in chunks.get("chunks", []):
            h = str(ch.get("sha256_chunk", ""))
            vec = PatchPack._hash_to_vec(h, dim)
            cid = f"{ch.get('file_path')}:{ch.get('byte_start')}-{ch.get('byte_end')}"
            vectors.append({"chunk_id": cid, "embedding": vec})
        return {"model": f"hash-{dim}", "dim": dim, "vectors": vectors}

    # ---------------- internals ----------------

    @staticmethod
    def _hash_to_vec(h: str, dim: int) -> List[float]:
        """
        Map a SHA256 hex digest to a fixed-length pseudo-embedding.
        Byte -> [-1.0, 1.0) via (b - 128)/128, repeated to `dim`.
        """
        try:
            raw = bytes.fromhex(h)
        except ValueError:
            raw = b"\x00" * 32  # deterministic fallback

        vals: List[float] = []
        i = 0
        L = max(1, len(raw))
        while len(vals) < dim:
            vals.append((raw[i % L] - 128) / 128.0)
            i += 1
        return vals
