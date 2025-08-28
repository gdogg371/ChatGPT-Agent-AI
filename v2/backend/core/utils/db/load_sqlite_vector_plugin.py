from __future__ import annotations
import sys, io, os, json, zipfile, tarfile, sqlite3 as _stdlib_sqlite3, urllib.request, urllib.error, subprocess
from pathlib import Path
from typing import Optional, Tuple, List

# ---- Repo-aware path resolution (fix leading "\" issue on Windows) -----------

HERE = Path(__file__).resolve()
PARENTS = list(HERE.parents)
try:
    _DEFAULT_REPO_ROOT = PARENTS[5]  # …/ChatGPT Bot/
except IndexError:
    _DEFAULT_REPO_ROOT = PARENTS[-1]

REPO_ROOT = Path(os.getenv("REPO_ROOT", str(_DEFAULT_REPO_ROOT))).resolve()

def _abs_repo(pathlike: str | Path) -> Path:
    """
    Resolve a path relative to the repo root unless it's a true absolute path.
    Treat leading '\' or '/' without drive (Windows) as repo-relative, not drive root.
    """
    s = str(pathlike)
    # Windows: "\foo\bar" -> repo-relative
    if os.name == "nt" and (s.startswith("\\") or s.startswith("/")) and not (len(s) >= 2 and s[1] == ":"):
        return (REPO_ROOT / s.lstrip("\\/")).resolve()
    p = Path(s)
    if not p.is_absolute():
        return (REPO_ROOT / p).resolve()
    return p

# ---- Config -----------------------------------------------------------------

# Previously these were absolute like r"/tests_adhoc" (wrong on Windows).
PLUGINS_DIR: Path = _abs_repo(os.getenv("SQLITE_PLUGINS_DIR", "tests_adhoc"))
SCHEMAS_DIR: Path = _abs_repo(os.getenv("SQLITE_SCHEMAS_DIR", "scripts/sqlite_sql_schemas"))

DEFAULT_DB = os.getenv(
    "SQLITE_DB_URL",
    r"sqlite:///C:/Users/cg371/PycharmProjects/ChatGPT Bot/databases/bot_dev.db"
)
DEFAULT_MIGRATIONS = ["053_create_introspection_index_fts.sql", "054_create_vector_index.sql"]

REPO_SQLITE_VEC = ["asg017/sqlite-vec"]                       # primary
REPO_SQLITE_VSS = ["ankane/sqlite-vss", "asg017/sqlite-vss"]  # optional

USER_AGENT = "vector-installer/1.2"
WINDOWS_DLL_SUFFIX = ".dll"

# ---- Helpers ----------------------------------------------------------------
def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def _http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))

def _http_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream"})
    with urllib.request.urlopen(req) as resp:
        return resp.read()

def _latest_assets(repo: str) -> List[dict]:
    api = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        data = _http_json(api); return data.get("assets", []) or []
    except Exception as e:
        print(f"[warn] release query failed for {repo}: {e}"); return []

def _score_win_asset(a: dict, want: str) -> int:
    name = a.get("name", "").lower()
    s = 0
    if "win" in name or "windows" in name: s += 8
    if name.endswith(".dll"): s += 10
    if name.endswith(".zip"): s += 6
    if name.endswith(".tar.gz"): s += 6
    if "x64" in name or "amd64" in name or "x86_64" in name: s += 3
    if want in name: s += 2
    if "amalgamation" in name or "source" in name or "src" in name: s -= 10
    if "darwin" in name or "macos" in name or "osx" in name or "linux" in name: s -= 6
    return s

def _pick_win_asset(assets: List[dict], want: str) -> Optional[dict]:
    if not assets: return None
    ranked = sorted(assets, key=lambda a: _score_win_asset(a, want), reverse=True)
    best = ranked[0]
    return best if _score_win_asset(best, want) > 0 else None

def _extract_dll_from_archive(data: bytes, fname: str, want_substring: str) -> Tuple[str, bytes]:
    fname_l = fname.lower()
    if fname_l.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            cands = [n for n in zf.namelist() if n.lower().endswith(".dll") and want_substring in n.lower()]
            if not cands:
                cands = [n for n in zf.namelist() if n.lower().endswith(".dll")]
            if not cands:
                raise FileNotFoundError("archive contained no .dll")
            pick = max(cands, key=len)
            return pick, zf.read(pick)
    if fname_l.endswith(".tar.gz"):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            dll_members = [m for m in tf.getmembers() if m.name.lower().endswith(".dll")]
            dll_members = [m for m in dll_members if want_substring in m.name.lower()] or dll_members
            if not dll_members:
                raise FileNotFoundError("archive contained no .dll")
            pick = max(dll_members, key=lambda m: len(m.name))
            extracted = tf.extractfile(pick)
            if not extracted:
                raise FileNotFoundError("failed to extract .dll from tar.gz")
            return pick.name, extracted.read()
    raise ValueError(f"unknown archive type: {fname}")

def _download_extension(repo_list: List[str], want: str, out_name: str) -> Optional[Path]:
    for repo in repo_list:
        assets = _latest_assets(repo)
        asset = _pick_win_asset(assets, want)
        if not asset: continue
        url = asset.get("browser_download_url"); name = asset.get("name", "")
        if not url: continue
        print(f"[dl] {repo} -> {name}")
        try:
            data = _http_bytes(url)
            _ensure_dir(PLUGINS_DIR)
            if name.lower().endswith((".zip", ".tar.gz")):
                inner_name, dll_bytes = _extract_dll_from_archive(data, name, want)
                dest = PLUGINS_DIR / out_name
                dest.write_bytes(dll_bytes)
                print(f"[ok] extracted {inner_name} -> {dest}")
            elif name.lower().endswith(".dll"):
                dest = PLUGINS_DIR / out_name
                dest.write_bytes(data)
                print(f"[ok] downloaded -> {dest}")
            else:
                print(f"[warn] unsupported asset type: {name}"); continue
            return dest
        except Exception as e:
            print(f"[warn] download/extract failed for {repo}: {e}")
    return None

def _pip_install(pkg: str) -> None:
    print(f"[pip] installing {pkg} …")
    res = subprocess.run([sys.executable, "-m", "pip", "install", pkg], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip())

def _get_sqlite3() -> tuple[object, Optional[Path]]:
    """
    Prefer pysqlite3-binary (ships sqlite3.dll). Returns (sqlite3_module, dll_dir_for_windows).
    """
    try:
        from pysqlite3 import dbapi2 as sqlite3  # type: ignore
        try:
            import pysqlite3 as _p
            dll_dir = Path(_p.__file__).resolve().parent
        except Exception:
            dll_dir = None
        print(f"[ok] using pysqlite3 (SQLite {sqlite3.sqlite_version})")
        return sqlite3, dll_dir
    except Exception:
        print("[info] pysqlite3-binary not available; attempting to install…")
        try:
            _pip_install("pysqlite3-binary")
            from pysqlite3 import dbapi2 as sqlite3  # type: ignore
            import pysqlite3 as _p
            dll_dir = Path(_p.__file__).resolve().parent
            print(f"[ok] using pysqlite3 (SQLite {sqlite3.sqlite_version})")
            return sqlite3, dll_dir
        except Exception as e:
            print(f"[warn] pysqlite3-binary install/use failed: {e}")
            print(f"[ok] falling back to stdlib sqlite3 (SQLite {_stdlib_sqlite3.sqlite_version})")
            return _stdlib_sqlite3, None

def _connect_sqlite(sqlite3_mod, db_url_or_path: Optional[str]) -> object:
    if not db_url_or_path or db_url_or_path.strip() == "":
        return sqlite3_mod.connect(":memory:")
    s = db_url_or_path.strip()
    if s.startswith("sqlite:///"):
        return sqlite3_mod.connect(s[len("sqlite:///"):])
    if s.startswith("sqlite:///:memory:"):
        return sqlite3_mod.connect(":memory:")
    return sqlite3_mod.connect(s)

def _pragma_modules(conn) -> List[str]:
    try:
        cur = conn.execute("PRAGMA module_list;")
        return [row[1] for row in cur.fetchall()]
    except Exception:
        return []

def _check_vec(conn) -> Tuple[bool, str]:
    try:
        mods = _pragma_modules(conn)
        if "vec0" in mods: return True, "vec0 in PRAGMA module_list"
        try:
            conn.execute("SELECT vec_version()").fetchone()
            return True, "vec_version() callable"
        except Exception:
            return True, "vec loaded (no vec_version())"
    except Exception as e:
        return False, f"vec probe exception: {e}"

def _check_vss(conn) -> Tuple[bool, str]:
    try:
        mods = _pragma_modules(conn)
        if "vss0" in mods: return True, "vss0 in PRAGMA module_list"
        try:
            conn.execute("CREATE VIRTUAL TABLE temp._vss_probe USING vss0(embedding( dim=3 ));")
            conn.execute("DROP TABLE temp._vss_probe;")
            return True, "vss0 vtable create/drop ok"
        except Exception as e:
            return False, f"vss0 create/drop failed: {e}"
    except Exception as e:
        return False, f"vss probe exception: {e}"

def _apply_migrations(conn, files: List[str]) -> None:
    for fname in files:
        path = SCHEMAS_DIR / fname
        if not path.exists():
            print(f"[warn] migration not found: {path}")
            continue
        sql = path.read_text(encoding="utf-8")
        try:
            conn.executescript(sql)
            print(f"[ok] applied migration: {fname}")
        except Exception as e:
            print(f"[warn] migration failed ({fname}): {e}")

# ---- Main -------------------------------------------------------------------
def install(db: Optional[str] = DEFAULT_DB, apply_migrations: bool = True) -> None:
    print(f"[init] repo root:          {REPO_ROOT}")
    print(f"[init] target plugins dir: {PLUGINS_DIR}")
    print(f"[init] schemas dir:        {SCHEMAS_DIR}")
    print(f"[init] database:           {db or ':memory:'}")

    if not SCHEMAS_DIR.exists():
        print(f"[error] schemas dir not found: {SCHEMAS_DIR}", file=sys.stderr)
        sys.exit(2)
    _ensure_dir(PLUGINS_DIR)

    # Download proper Windows assets and extract DLLs
    vec_dll = _download_extension(REPO_SQLITE_VEC, "vec", "vec0.dll")
    if not vec_dll:
        print("[warn] sqlite-vec Windows DLL not found; will continue without it.")
    vss_dll = _download_extension(REPO_SQLITE_VSS, "vss", "vss0.dll")
    if not vss_dll:
        print("[warn] sqlite-vss Windows DLL not found; continuing without vss.")

    # Prefer pysqlite3 runtime (brings its own sqlite3.dll)
    sqlite3_mod, dll_dir = _get_sqlite3()

    # Help Windows find dependent DLLs (sqlite3.dll, etc.)
    if os.name == "nt":
        try:
            os.add_dll_directory(str(PLUGINS_DIR))
        except Exception:
            pass
        if dll_dir:
            try:
                os.add_dll_directory(str(dll_dir))
            except Exception:
                pass

    con = _connect_sqlite(sqlite3_mod, db)
    try:
        con.enable_load_extension(True)
    except Exception:
        pass

    # Load vec first
    if vec_dll and vec_dll.exists():
        try:
            con.load_extension(str(vec_dll))
            ok, msg = _check_vec(con); print(f"[check] vec: {'OK' if ok else 'FAIL'} — {msg}")
        except Exception as e:
            print(f"[warn] failed loading vec ({vec_dll.name}): {e}")
    else:
        print("[info] vec extension not loaded (no DLL).")

    # Load vss (optional)
    if vss_dll and vss_dll.exists():
        try:
            con.load_extension(str(vss_dll))
            ok, msg = _check_vss(con); print(f"[check] vss: {'OK' if ok else 'FAIL'} — {msg}")
        except Exception as e:
            print(f"[warn] failed loading vss ({vss_dll.name}): {e}")
    else:
        print("[info] vss extension not loaded (no DLL).")

    if apply_migrations:
        _apply_migrations(con, DEFAULT_MIGRATIONS)

    con.commit(); con.close()
    print("[done] Completed. DB unchanged; extensions optional; migrations applied if requested.")

# ---- Autorun for PyCharm -----------------------------------------------------
if __name__ == "__main__":
    # No args → auto-run with defaults (press Run in PyCharm)
    install(db=DEFAULT_DB, apply_migrations=True)





