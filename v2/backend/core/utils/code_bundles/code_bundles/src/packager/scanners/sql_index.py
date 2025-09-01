# File: v2/backend/core/utils/code_bundles/code_bundles/sql_index.py
"""
SQL indexer (stdlib-only).

Extracts lightweight metadata from .sql files without external parsers.

Per-file record (example)
-------------------------
{
  "kind": "sql.index",
  "path": "migrations/001_init.sql",
  "size": 8421,
  "statements": 17,
  "kinds": {"select": 4, "insert": 1, "update": 0, "delete": 0, "create_table": 2, ...},
  "tables": {
    "referenced": ["public.users","orders"],
    "created": ["public.users"],
    "altered": [],
    "dropped": [],
    "views": ["active_users"],
    "indexes_on": ["public.users"]
  },
  "constraints": {"foreign_keys": 2, "primary_keys": 1, "unique": 1, "checks": 0},
  "functions": {"created": ["fn_total"], "procedures": [], "triggers": ["tr_users_audit"]},
  "params": {"named": [":user_id","@tenant"], "positional": ["$1","$2"], "qmark": 3},
  "danger": {
    "drop_database": 0,
    "drop_table": 1,
    "truncate": 0,
    "delete_no_where": 0,
    "update_no_where": 0
  },
  "dialect_hints": ["postgresql","mysql"]
}

Summary record
--------------
{
  "kind": "sql.index.summary",
  "files": 12,
  "statements": 203,
  "kinds": {... aggregated ...},
  "tables": {"unique_referenced": 31, "created": 9, "dropped": 1},
  "danger_total": 2,
  "top_tables": [{"table":"public.users","refs":11}, ...]
}

Notes
-----
* This is a pragmatic, dependency-free scanner. It will not be perfect for
  all dialects, but it captures useful signals for most SQL you’ll see in repos.
* Paths returned are repo-relative POSIX. If your pipeline distinguishes local
  vs GitHub path modes, map `path` before appending to the manifest.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

RepoItem = Tuple[Path, str]  # (local_path, repo_relative_posix)

_MAX_READ_BYTES = 2 * 1024 * 1024  # 2 MiB safety cap
_MAX_LIST = 200                    # cap lists we keep per file


# ──────────────────────────────────────────────────────────────────────────────
# IO helpers
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


# ──────────────────────────────────────────────────────────────────────────────
# SQL comment stripping and statement splitting (string-aware)
# ──────────────────────────────────────────────────────────────────────────────

def _strip_sql_comments(text: str) -> str:
    """
    Remove SQL comments while preserving string literals.

    Supports:
      -- line comments
      #  line comments (MySQL)
      /* block comments */
    """
    out_chars: List[str] = []
    i = 0
    n = len(text)
    in_single = False
    in_double = False
    in_back = False  # backtick (MySQL identifiers)
    while i < n:
        ch = text[i]

        # Toggle string states
        if ch == "'" and not in_double and not in_back:
            # Check escaped single quote '' (SQL) or \' (some dialects)
            if in_single:
                # Peek previous char for backslash
                prev = text[i - 1] if i > 0 else ""
                if prev == "\\":
                    out_chars.append(ch)
                    i += 1
                    continue
                # Double single-quote escape: if next char is also ', consume one and keep both
                if i + 1 < n and text[i + 1] == "'":
                    out_chars.extend(["'", "'"])
                    i += 2
                    continue
                in_single = False
                out_chars.append(ch)
                i += 1
                continue
            else:
                in_single = True
                out_chars.append(ch)
                i += 1
                continue

        if ch == '"' and not in_single and not in_back:
            if in_double:
                prev = text[i - 1] if i > 0 else ""
                if prev == "\\":
                    out_chars.append(ch)
                    i += 1
                    continue
                in_double = False
                out_chars.append(ch)
                i += 1
                continue
            else:
                in_double = True
                out_chars.append(ch)
                i += 1
                continue

        if ch == "`" and not in_single and not in_double:
            in_back = not in_back
            out_chars.append(ch)
            i += 1
            continue

        # If inside a string/quoted identifier, just copy
        if in_single or in_double or in_back:
            out_chars.append(ch)
            i += 1
            continue

        # Not inside a string: check for comments
        if ch == "-" and i + 1 < n and text[i + 1] == "-":
            # Consume until end of line
            i += 2
            while i < n and text[i] not in ("\n", "\r"):
                i += 1
            # Keep the newline
            out_chars.append("\n")
            i += 1
            continue

        if ch == "#":
            # MySQL-style line comment
            i += 1
            while i < n and text[i] not in ("\n", "\r"):
                i += 1
            out_chars.append("\n")
            i += 1
            continue

        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            # Block comment
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2 if i + 1 < n else 1
            out_chars.append(" ")
            continue

        out_chars.append(ch)
        i += 1

    return "".join(out_chars)


def _split_sql_statements(text: str) -> List[str]:
    """
    Split SQL text into statements at semicolons, ignoring semicolons that
    appear inside string literals or backtick identifiers.
    """
    stmts: List[str] = []
    buf: List[str] = []
    in_single = in_double = in_back = False
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]

        if ch == "'" and not in_double and not in_back:
            # toggle single, handle doubled ''
            if in_single:
                if i + 1 < n and text[i + 1] == "'":
                    buf.extend(["'", "'"])
                    i += 2
                    continue
                in_single = False
            else:
                in_single = True
            buf.append(ch)
            i += 1
            continue

        if ch == '"' and not in_single and not in_back:
            in_double = not in_double
            buf.append(ch)
            i += 1
            continue

        if ch == "`" and not in_single and not in_double:
            in_back = not in_back
            buf.append(ch)
            i += 1
            continue

        if ch == ";" and not (in_single or in_double or in_back):
            # end of statement
            stmt = "".join(buf).strip()
            if stmt:
                stmts.append(stmt)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts


# ──────────────────────────────────────────────────────────────────────────────
# Extraction helpers
# ──────────────────────────────────────────────────────────────────────────────

# Table-ish identifier: optional schema, quoted or unquoted
_ID = r"(?:`[^`]+`|\"[^\"]+\"|\[[^\]]+\]|[A-Za-z_][A-Za-z0-9_\$]*)"
_TABLE = rf"({_ID}(?:\s*\.\s*{_ID})?)"

_RE_FROM = re.compile(rf"\bFROM\s+{_TABLE}", re.IGNORECASE)
_RE_JOIN = re.compile(rf"\bJOIN\s+{_TABLE}", re.IGNORECASE)
_RE_INSERT_INTO = re.compile(rf"\bINSERT\s+INTO\s+{_TABLE}", re.IGNORECASE)
_RE_UPDATE = re.compile(rf"\bUPDATE\s+{_TABLE}", re.IGNORECASE)
_RE_DELETE_FROM = re.compile(rf"\bDELETE\s+FROM\s+{_TABLE}", re.IGNORECASE)
_RE_CREATE_TABLE = re.compile(rf"\bCREATE\s+(?:TEMP|TEMPORARY\s+)?TABLE\b(?:\s+IF\s+NOT\s+EXISTS)?\s+{_TABLE}", re.IGNORECASE)
_RE_ALTER_TABLE = re.compile(rf"\bALTER\s+TABLE\s+{_TABLE}", re.IGNORECASE)
_RE_DROP_TABLE = re.compile(rf"\bDROP\s+TABLE\b(?:\s+IF\s+EXISTS)?\s+{_TABLE}", re.IGNORECASE)
_RE_CREATE_VIEW = re.compile(rf"\bCREATE\s+VIEW\b(?:\s+IF\s+NOT\s+EXISTS)?\s+{_TABLE}", re.IGNORECASE)
_RE_CREATE_INDEX = re.compile(rf"\bCREATE\s+(?:UNIQUE\s+)?INDEX\b.*?\bON\s+{_TABLE}", re.IGNORECASE | re.DOTALL)
_RE_TRUNCATE = re.compile(rf"\bTRUNCATE\s+TABLE?\s+{_TABLE}", re.IGNORECASE)
_RE_DROP_DATABASE = re.compile(r"\bDROP\s+DATABASE\b", re.IGNORECASE)

_RE_FK = re.compile(r"\bFOREIGN\s+KEY\b", re.IGNORECASE)
_RE_PK = re.compile(r"\bPRIMARY\s+KEY\b", re.IGNORECASE)
_RE_UNIQUE = re.compile(r"\bUNIQUE\b", re.IGNORECASE)
_RE_CHECK = re.compile(r"\bCHECK\s*\(", re.IGNORECASE)

# Danger patterns: DML without WHERE
_RE_DELETE_NO_WHERE = re.compile(r"\bDELETE\s+FROM\b(?!.*\bWHERE\b)", re.IGNORECASE | re.DOTALL)
_RE_UPDATE_NO_WHERE = re.compile(r"\bUPDATE\b\s+.+?(?!.*\bWHERE\b).*?\bSET\b", re.IGNORECASE | re.DOTALL)

# Dialect hints
_DIALECT_HINTS = {
    "postgresql": [r"\bSERIAL\b", r"\bBIGSERIAL\b", r"\bILIKE\b", r"\bRETURNING\b", r"\b::\w+\b", r"\bON\s+CONFLICT\b"],
    "mysql": [r"\bAUTO_INCREMENT\b", r"\bUNSIGNED\b", r"\bENGINE\s*=", r"`[^`]+`"],
    "sqlite": [r"\bINTEGER\s+PRIMARY\s+KEY\b", r"\bAUTOINCREMENT\b", r"\bWITHOUT\s+ROWID\b"],
    "mssql": [r"\bIDENTITY\s*\(", r"\bNVARCHAR\b", r"\bTOP\s+\d+\b", r"\[.+?\]"],
    "oracle": [r"\bNUMBER\(", r"\bNVL\(", r"\bSYSTIMESTAMP\b", r"\bMERGE\s+INTO\b"],
}

# Parameters/placeholders
_RE_NAMED_COLON = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")  # :name (avoid ::cast)
_RE_NAMED_AT = re.compile(r"@([A-Za-z_][A-Za-z0-9_]*)")          # @name
_RE_POS_DOLLAR = re.compile(r"\$([0-9]+)")                       # $1, $2
_RE_QMARK = re.compile(r"\?")                                    # ?


def _clean_ident(ident: str) -> str:
    s = ident.strip()
    if s.startswith(("`", '"', "[")):
        # strip matching quotes/brackets
        if s.startswith("`") and s.endswith("`"):
            return s[1:-1]
        if s.startswith('"') and s.endswith('"'):
            return s[1:-1]
        if s.startswith("[") and s.endswith("]"):
            return s[1:-1]
    return re.sub(r"\s*", "", s)


def _collect_tables_from_stmt(stmt: str) -> Dict[str, Set[str]]:
    refs: Set[str] = set()
    created: Set[str] = set()
    altered: Set[str] = set()
    dropped: Set[str] = set()
    views: Set[str] = set()
    indexes_on: Set[str] = set()

    for rx in (_RE_FROM, _RE_JOIN, _RE_INSERT_INTO, _RE_UPDATE, _RE_DELETE_FROM):
        for m in rx.finditer(stmt):
            refs.add(_clean_ident(m.group(1)))

    for m in _RE_CREATE_TABLE.finditer(stmt):
        created.add(_clean_ident(m.group(1)))
    for m in _RE_ALTER_TABLE.finditer(stmt):
        altered.add(_clean_ident(m.group(1)))
    for m in _RE_DROP_TABLE.finditer(stmt):
        dropped.add(_clean_ident(m.group(1)))
    for m in _RE_CREATE_VIEW.finditer(stmt):
        views.add(_clean_ident(m.group(1)))
    for m in _RE_CREATE_INDEX.finditer(stmt):
        indexes_on.add(_clean_ident(m.group(1)))

    return {
        "referenced": refs,
        "created": created,
        "altered": altered,
        "dropped": dropped,
        "views": views,
        "indexes_on": indexes_on,
    }


def _classify_stmt(stmt_upper: str) -> str:
    # Order matters for CREATE variants
    if stmt_upper.startswith("CREATE TABLE"):
        return "create_table"
    if stmt_upper.startswith("CREATE VIEW"):
        return "create_view"
    if stmt_upper.startswith("CREATE INDEX") or " CREATE UNIQUE INDEX" in stmt_upper:
        return "create_index"
    if stmt_upper.startswith("ALTER TABLE"):
        return "alter_table"
    if stmt_upper.startswith("DROP TABLE"):
        return "drop_table"
    if stmt_upper.startswith("TRUNCATE"):
        return "truncate"
    if stmt_upper.startswith("INSERT"):
        return "insert"
    if stmt_upper.startswith("UPDATE"):
        return "update"
    if stmt_upper.startswith("DELETE"):
        return "delete"
    if stmt_upper.startswith("SELECT") or stmt_upper.startswith("WITH"):
        return "select"
    if stmt_upper.startswith("CREATE FUNCTION") or stmt_upper.startswith("CREATE OR REPLACE FUNCTION"):
        return "create_function"
    if stmt_upper.startswith("CREATE PROCEDURE") or "CREATE OR REPLACE PROCEDURE" in stmt_upper:
        return "create_procedure"
    if " CREATE TRIGGER" in stmt_upper or stmt_upper.startswith("CREATE TRIGGER"):
        return "create_trigger"
    if stmt_upper.startswith("DROP DATABASE"):
        return "drop_database"
    if stmt_upper.startswith("DROP VIEW"):
        return "drop_view"
    if stmt_upper.startswith("DROP INDEX"):
        return "drop_index"
    return "other"


def _dialect_hints(stmt: str) -> Set[str]:
    hits: Set[str] = set()
    for name, pats in _DIALECT_HINTS.items():
        for p in pats:
            if re.search(p, stmt, flags=re.IGNORECASE):
                hits.add(name)
                break
    return hits


def _params_in_stmt(stmt: str) -> Tuple[Set[str], Set[str], int]:
    # Avoid ::type casts triggering :type; quick suppression by removing ::word first
    stmt_ = re.sub(r"::\w+", "", stmt)

    named = set([f":{m.group(1)}" for m in _RE_NAMED_COLON.finditer(stmt_)])
    named.update([f"@{m.group(1)}" for m in _RE_NAMED_AT.finditer(stmt_)])
    pos = set([f"${m.group(1)}" for m in _RE_POS_DOLLAR.finditer(stmt_)])
    qmarks = len(_RE_QMARK.findall(stmt_))
    return named, pos, qmarks


def _constraints_in_stmt(stmt: str) -> Dict[str, int]:
    return {
        "foreign_keys": len(_RE_FK.findall(stmt)),
        "primary_keys": len(_RE_PK.findall(stmt)),
        "unique": len(_RE_UNIQUE.findall(stmt)),
        "checks": len(_RE_CHECK.findall(stmt)),
    }


def _functions_in_stmt(stmt_upper: str, stmt: str) -> Dict[str, List[str]]:
    created_funcs: List[str] = []
    created_procs: List[str] = []
    created_triggers: List[str] = []

    # crude name extraction: CREATE FUNCTION <name> or CREATE OR REPLACE FUNCTION <name>
    m = re.search(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+([A-Za-z_][\w\.\$]*)", stmt_upper, flags=re.IGNORECASE)
    if m:
        created_funcs.append(m.group(1))

    m = re.search(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?PROCEDURE\s+([A-Za-z_][\w\.\$]*)", stmt_upper, flags=re.IGNORECASE)
    if m:
        created_procs.append(m.group(1))

    m = re.search(r"\bCREATE\s+TRIGGER\s+([A-Za-z_][\w\.\$]*)", stmt_upper, flags=re.IGNORECASE)
    if m:
        created_triggers.append(m.group(1))

    return {
        "created": created_funcs[:_MAX_LIST],
        "procedures": created_procs[:_MAX_LIST],
        "triggers": created_triggers[:_MAX_LIST],
    }


def _danger_flags(stmt_upper: str, stmt: str) -> Dict[str, int]:
    return {
        "drop_database": 1 if _RE_DROP_DATABASE.search(stmt_upper) else 0,
        "drop_table": 1 if _RE_DROP_TABLE.search(stmt_upper) else 0,
        "truncate": 1 if _RE_TRUNCATE.search(stmt_upper) else 0,
        "delete_no_where": 1 if _RE_DELETE_NO_WHERE.search(stmt_upper) else 0,
        "update_no_where": 1 if _RE_UPDATE_NO_WHERE.search(stmt_upper) else 0,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Public API: per-file analysis
# ──────────────────────────────────────────────────────────────────────────────

def analyze_file(*, local_path: Path, repo_rel_posix: str) -> Dict:
    """
    Analyze a single SQL file and return a JSON-ready record.
    """
    text_raw = _read_text_limited(local_path)
    try:
        size = local_path.stat().st_size
    except Exception:
        size = len(text_raw.encode("utf-8", errors="ignore"))

    text = _strip_sql_comments(text_raw)
    statements = _split_sql_statements(text)

    kinds_counter: Counter[str] = Counter()
    refs_all: Set[str] = set()
    created_all: Set[str] = set()
    altered_all: Set[str] = set()
    dropped_all: Set[str] = set()
    views_all: Set[str] = set()
    indexes_on_all: Set[str] = set()
    fk_total = pk_total = uniq_total = chk_total = 0
    dialect_hits: Set[str] = set()
    named_params: Set[str] = set()
    pos_params: Set[str] = set()
    qmark_total = 0

    functions_created: List[str] = []
    procedures_created: List[str] = []
    triggers_created: List[str] = []

    danger_agg = {"drop_database": 0, "drop_table": 0, "truncate": 0, "delete_no_where": 0, "update_no_where": 0}

    for stmt in statements:
        stmt_upper = stmt.strip().upper()
        kind = _classify_stmt(stmt_upper)
        kinds_counter[kind] += 1

        # Tables
        tinfo = _collect_tables_from_stmt(stmt)
        refs_all.update(tinfo["referenced"])
        created_all.update(tinfo["created"])
        altered_all.update(tinfo["altered"])
        dropped_all.update(tinfo["dropped"])
        views_all.update(tinfo["views"])
        indexes_on_all.update(tinfo["indexes_on"])

        # Constraints
        cns = _constraints_in_stmt(stmt)
        fk_total += cns["foreign_keys"]
        pk_total += cns["primary_keys"]
        uniq_total += cns["unique"]
        chk_total += cns["checks"]

        # Dialect hints
        dialect_hits.update(_dialect_hints(stmt))

        # Parameters
        n_named, n_pos, qn = _params_in_stmt(stmt)
        named_params.update(n_named)
        pos_params.update(n_pos)
        qmark_total += qn

        # Functions/procedures/triggers
        fn = _functions_in_stmt(stmt_upper, stmt)
        functions_created.extend(fn["created"])
        procedures_created.extend(fn["procedures"])
        triggers_created.extend(fn["triggers"])

        # Danger
        flags = _danger_flags(stmt_upper, stmt)
        for k in danger_agg.keys():
            danger_agg[k] += int(flags.get(k, 0))

    rec = {
        "kind": "sql.index",
        "path": repo_rel_posix,
        "size": int(size),
        "statements": len(statements),
        "kinds": dict(kinds_counter),
        "tables": {
            "referenced": sorted(list(refs_all))[:_MAX_LIST],
            "created": sorted(list(created_all))[:_MAX_LIST],
            "altered": sorted(list(altered_all))[:_MAX_LIST],
            "dropped": sorted(list(dropped_all))[:_MAX_LIST],
            "views": sorted(list(views_all))[:_MAX_LIST],
            "indexes_on": sorted(list(indexes_on_all))[:_MAX_LIST],
        },
        "constraints": {
            "foreign_keys": int(fk_total),
            "primary_keys": int(pk_total),
            "unique": int(uniq_total),
            "checks": int(chk_total),
        },
        "functions": {
            "created": functions_created[:_MAX_LIST],
            "procedures": procedures_created[:_MAX_LIST],
            "triggers": triggers_created[:_MAX_LIST],
        },
        "params": {
            "named": sorted(list(named_params))[:_MAX_LIST],
            "positional": sorted(list(pos_params))[:_MAX_LIST],
            "qmark": int(qmark_total),
        },
        "danger": {k: int(v) for k, v in danger_agg.items()},
        "dialect_hints": sorted(list(dialect_hits))[:10],
    }
    return rec


# ──────────────────────────────────────────────────────────────────────────────
# Public API: repository scan with summary
# ──────────────────────────────────────────────────────────────────────────────

def scan(repo_root: Path, discovered: Iterable[RepoItem]) -> List[Dict]:
    """
    Scan discovered files, indexing .sql files and returning:
      - One 'sql.index' record per SQL file
      - One 'sql.index.summary' record at the end
    """
    files = [(lp, rel) for (lp, rel) in discovered if rel.lower().endswith(".sql")]
    results: List[Dict] = []

    kinds_total: Counter[str] = Counter()
    all_tables_ref: Counter[str] = Counter()
    created_all: Set[str] = set()
    dropped_all: Set[str] = set()
    danger_total = 0
    stmts_total = 0

    for local, rel in files:
        rec = analyze_file(local_path=local, repo_rel_posix=rel)
        results.append(rec)

        stmts_total += int(rec.get("statements", 0))
        kinds_total.update(rec.get("kinds", {}))

        tables = rec.get("tables", {})
        for t in tables.get("referenced", []) or []:
            all_tables_ref[t] += 1
        for t in tables.get("created", []) or []:
            created_all.add(t)
        for t in tables.get("dropped", []) or []:
            dropped_all.add(t)

        danger = rec.get("danger", {}) or {}
        danger_total += sum(int(v or 0) for v in danger.values())

    top_tables = [{"table": t, "refs": c} for (t, c) in all_tables_ref.most_common(20)]

    summary = {
        "kind": "sql.index.summary",
        "files": len(files),
        "statements": int(stmts_total),
        "kinds": dict(kinds_total),
        "tables": {
            "unique_referenced": len(all_tables_ref),
            "created": len(created_all),
            "dropped": len(dropped_all),
            "top_referenced": top_tables,
        },
        "danger_total": int(danger_total),
    }
    results.append(summary)
    return results


__all__ = ["scan", "analyze_file"]
