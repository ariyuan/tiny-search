#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""index_cache.py — index persistence and incremental warm start.

Architecture:
  - Storage: SQLite (stdlib), one row per directory (path, seq, mtime, names_blob).
    names_blob = a zlib-compressed list of basenames (files + junctions, same semantics as
    build_walk_index). Measured: 286,190 paths -> 3.8 MB of compressed blocks, ~8 MB
    including 40k directory paths (row-per-file storage would need 73 MB).
  - Warm start: load all rows -> stat each directory and compare mtime -> rescan only the
    changed directories (recursively expanding newly discovered subdirectories) -> merge.
    Measured on the author's machine: ~1.1 s (0.33 s load+decompress, 0.74 s for 40k stat
    calls), and no write at all when nothing changed.
  - Cold start: a full walk returns synchronously; persistence happens on a background
    daemon thread (single transaction), the service does not wait.
  - The cache is only built for fixed local NTFS volumes; a corrupt db, an old schema, an
    unwritable location or a lock timeout all degrade gracefully (rebuild or skip the cache)
    and never crash.
"""

import hashlib
import os
import sqlite3
import sys
import threading
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mft_finder import build_walk_index, scan_dir, walk_with_dirs  # noqa: E402

SCHEMA_VERSION = "1"
BUSY_TIMEOUT_MS = 1000
DRIVE_FIXED = 3

# ---------------------------------------------------------------- volume detection


def _volume_info(path: str):
    """(drive_type, fs_name). Returns (None, None) for UNC paths and errors."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    k32.GetDriveTypeW.restype = wintypes.UINT
    k32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD,
    ]
    k32.GetVolumeInformationW.restype = wintypes.BOOL

    root = os.path.splitdrive(os.path.abspath(path))[0]
    if not root:
        return None, None
    drive = root + "\\"
    dtype = k32.GetDriveTypeW(drive)
    fs = ctypes.create_unicode_buffer(32)
    ok = k32.GetVolumeInformationW(drive, None, 0, None, None, None, fs, 32)
    return dtype, (fs.value if ok else None)


def is_cacheable(root: str) -> bool:
    """Cache only fixed local NTFS volumes: mtime semantics are unreliable on network
    shares, removable drives and FAT32."""
    try:
        dtype, fs = _volume_info(root)
    except OSError:
        return False
    return dtype == DRIVE_FIXED and (fs or "").upper() == "NTFS"


# ---------------------------------------------------------------- cache file location


def cache_db_path(root: str, skip_appdata: bool, cache_dir: str = None) -> Path:
    """<cache_dir or project>/cache/index-<sha1(root|rules)[:12]>.db."""
    key = "%s|%s" % (os.path.abspath(root), "lean" if skip_appdata else "full")
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    base = Path(cache_dir) if cache_dir else (
        Path(__file__).resolve().parent.parent / "cache")
    return base / ("index-%s.db" % digest)


def _ensure_cache_dir(db_path: Path) -> bool:
    """Make sure the cache directory exists; returns False when it is read-only (no cache)."""
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- compression


def compress_names(names) -> bytes:
    """List of basenames -> zlib blob (level 6: 286k paths take 0.59 s -> 3.8 MB)."""
    return zlib.compress("\n".join(names).encode("utf-8"), 6)


def decompress_names(blob: bytes):
    """zlib blob -> list of basenames. Empty directory -> [].

    Note that `"".split("\\n") == ['']`, so the empty string needs a special case —
    otherwise an empty directory yields one ghost entry named "" (which becomes "dir\\"
    after joining) and a warm start invents files out of nowhere.
    """
    if not blob:
        return []
    text = zlib.decompress(blob).decode("utf-8")
    return text.split("\n") if text else []


# ---------------------------------------------------------------- IndexCache


class IndexCache:
    """Thin wrapper around one cache db. The connection belongs to the creating thread and
    is never shared across threads."""

    def __init__(self, db_path: Path):
        self.path = db_path
        self.con = sqlite3.connect(str(db_path), timeout=BUSY_TIMEOUT_MS / 1000.0)
        try:
            self.con.execute("PRAGMA journal_mode=OFF")
            self.con.execute("PRAGMA synchronous=OFF")
        except sqlite3.Error:
            # A garbage file blows up during connect (journal_mode reads the db header).
            # At that point self.con exists but the constructor never finished, so the
            # caller has no object to close — clean up here, otherwise the handle stays
            # open on Windows and _discard cannot delete the file.
            self.close()
            raise

    def healthy(self) -> bool:
        """quick_check liveness probe; garbage or half-written files always count as corrupt."""
        try:
            row = self.con.execute("PRAGMA quick_check(1)").fetchone()
            return bool(row) and row[0] == "ok"
        except sqlite3.DatabaseError:
            return False

    def ensure_schema(self):
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS dirs ("
            " path TEXT PRIMARY KEY, seq INTEGER NOT NULL,"
            " mtime REAL NOT NULL, names BLOB NOT NULL) WITHOUT ROWID")
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")

    def read_meta(self) -> dict:
        try:
            return dict(self.con.execute("SELECT key, value FROM meta").fetchall())
        except sqlite3.DatabaseError:
            return {}

    def load_rows(self):
        """[(path, seq, mtime, names_blob)] ordered by seq (= DFS visit order of the cold build)."""
        return self.con.execute(
            "SELECT path, seq, mtime, names FROM dirs ORDER BY seq").fetchall()

    def upsert_dir(self, path: str, seq: int, mtime: float, names_blob: bytes):
        self.con.execute(
            "INSERT OR REPLACE INTO dirs (path, seq, mtime, names) VALUES (?,?,?,?)",
            (path, seq, mtime, names_blob))

    def delete_dirs(self, paths):
        self.con.executemany("DELETE FROM dirs WHERE path=?", [(p,) for p in paths])

    def replace_all(self, rows):
        """Full replacement in a single transaction. The caller wraps this in `with`.
        rows = [(path, seq, mtime, blob)]."""
        self.con.execute("DELETE FROM dirs")
        self.con.executemany(
            "INSERT INTO dirs (path, seq, mtime, names) VALUES (?,?,?,?)", rows)

    def write_meta(self, meta: dict):
        self.con.executemany(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
            [(k, str(v)) for k, v in meta.items()])

    def commit(self):
        self.con.commit()

    def close(self):
        try:
            self.con.close()
        except sqlite3.Error:
            pass


# ---------------------------------------------------------------- diff details


def _rescan(path: str, known: dict, skip_appdata_child: bool = False):
    """Rescan one directory. Returns (names, new_rows):

    - names    the current files + junction names of this directory (same semantics as scan_dir)
    - new_rows [(subdir, mtime, names)] covering only subdirectories that are not in the
               cache yet, each scanned recursively (handles a new subtree created while the
               tool was offline). Known subdirectories keep their old row; their own mtime
               decides whether they get rescanned, so no work is duplicated.
    """
    names, subdirs = scan_dir(path)
    if skip_appdata_child:
        subdirs = [s for s in subdirs if os.path.basename(s).lower() != "appdata"]
    new_rows = []
    for s in subdirs:
        if s in known:
            continue
        sub_names, sub_rows = _rescan(s, known)
        try:
            m = os.stat(s).st_mtime
        except OSError:
            continue
        new_rows.append((s, m, sub_names))
        new_rows.extend(sub_rows)
    return names, new_rows


# ---------------------------------------------------------------- public entry points


def build_index_cached(root: str, skip_appdata: bool = False,
                       cache_dir: str = None, background: bool = True):
    """Unified index entry point: cache first, degrade on failure. The returned list has the
    same semantics as build_walk_index.

    Returns (files, stats). stats:
      mode      warm (cache diff) / cold (full rebuild) / nocache (no cache)
      load_ms / diff_ms / rescanned / entries / reason (for nocache)
    """
    stats = {"mode": "cold", "load_ms": 0.0, "diff_ms": 0.0,
             "rescanned": 0, "entries": 0, "reason": ""}

    def _no_cache(reason: str):
        files = build_walk_index(root, skip_appdata=skip_appdata)
        stats.update(mode="nocache", entries=len(files), reason=reason)
        return files, stats

    if not is_cacheable(root):
        return _no_cache("not a fixed NTFS volume")

    db_path = cache_db_path(root, skip_appdata, cache_dir)
    if not _ensure_cache_dir(db_path):
        return _no_cache("cache directory unavailable: %s" % db_path.parent)

    want_meta = {"schema": SCHEMA_VERSION, "root": os.path.abspath(root),
                 "rules": "lean" if skip_appdata else "full"}

    # Note: sqlite3.connect creates a 0-byte file as a side effect, so "is there a cache"
    # must be decided before connecting — otherwise the very first start would be mistaken
    # for a cache mismatch and print a warning.
    has_cache = db_path.exists() and db_path.stat().st_size > 0

    cache = None
    try:
        if not has_cache:
            # Brand-new cache: go straight to a cold build, no warning (the normal first run).
            return _cold_build(root, skip_appdata, db_path, want_meta, stats, background)
        cache = IndexCache(db_path)
        if not cache.healthy():
            raise ValueError("cache file is corrupt")
        if cache.read_meta() != want_meta:
            raise ValueError("cache does not match (version/root/rules)")
    except (sqlite3.Error, ValueError, OSError) as exc:
        if cache is not None:
            cache.close()
        _discard(db_path)
        print("[cache] %s, rebuilding from scratch." % exc)
        return _cold_build(root, skip_appdata, db_path, want_meta, stats, background)

    try:
        return _warm_build(root, skip_appdata, cache, stats)
    except sqlite3.Error as exc:
        print("[cache] Warm start failed (%s), rebuilding from scratch." % exc)
        return _cold_build(root, skip_appdata, db_path, want_meta, stats, background)
    finally:
        cache.close()


def print_cache_line(stats: dict, prefix: str = "[cache]"):
    """One-line cache status for CLI / service startup: how this run went, in one sentence."""
    mode = stats.get("mode", "nocache")
    if mode == "warm":
        print("%s warm start: loaded in %.0f ms + verified in %.0f ms, rescanned %s changed "
              "directories, %s entries" % (prefix, stats["load_ms"], stats["diff_ms"],
                                           format(stats["rescanned"], ","),
                                           format(stats["entries"], ",")))
    elif mode == "cold":
        print("%s cold start: full walk of %s entries (writing the cache now)"
              % (prefix, format(stats["entries"], ",")))
    else:
        print("%s no cache (%s), full walk this time" % (prefix, stats.get("reason", "")))


def _discard(db_path: Path):
    """Delete an untrusted cache file (reversible: the next run rebuilds it)."""
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(str(db_path) + suffix).unlink()
        except OSError:
            pass


def _cold_build(root, skip_appdata, db_path, want_meta, stats, background):
    """Cold start: a single walk returns the file list synchronously; writing happens on a
    background daemon thread.

    walk_with_dirs collects files and dir_rows in one traversal, so a cold start only
    touches the disk once and persistence reuses the same data (an early implementation ran
    build + persist separately and paid the IO twice).

    The thread handle is stored in stats["writer"] and callers should call wait_writer before
    the process exits — otherwise one-shot mode (--list) exits immediately, the daemon thread
    is cut short and the cache is never built.
    """
    files, dir_rows = walk_with_dirs(root, skip_appdata=skip_appdata)
    stats.update(mode="cold", entries=len(files))
    if background:
        t = threading.Thread(target=_persist_rows,
                             args=(db_path, want_meta, dir_rows),
                             daemon=True, name="index-cache-writer")
        t.start()
        stats["writer"] = t
    else:
        _persist_rows(db_path, want_meta, dir_rows)
    return files, stats


def wait_writer(stats: dict, timeout: float = 60.0):
    """Wait for the cold-start persistence thread (call before the process exits).

    A timeout or a missing thread is silently ignored — the cache is an optional speed-up
    and must never block shutdown.
    """
    t = stats.get("writer")
    if t is not None:
        t.join(timeout)


def _persist_rows(db_path, want_meta, dir_rows):
    """Background daemon thread: directory rows -> single-transaction write. Failures only
    warn and never break the main flow.

    Rows are sorted by path before inserting: the WITHOUT ROWID B-tree is keyed by path, so
    inserting in random (DFS) key order keeps splitting pages — 20.4 MB measured — while
    path order stores the same data in 12.1 MB.
    """
    try:
        rows = [(p, i, m, compress_names(ns))
                for i, (p, m, ns) in enumerate(dir_rows)]
        rows.sort(key=lambda r: r[0])
        cache = IndexCache(db_path)
        try:
            cache.ensure_schema()
            with cache.con:
                cache.replace_all(rows)
                cache.write_meta(want_meta)
        finally:
            cache.close()
    except Exception as exc:  # the cache is an optional speed-up; never break the main flow
        print("[cache] Background write failed (%s), the next start will rebuild." % exc)


def _join_fast(d: str, n: str) -> str:
    """Character-for-character identical to os.path.join(d, n) for a bare file name, but
    without the split/checks inside join (286k calls add up to ~200 ms, and the warm-start
    budget is tight).

    Replicates the two special cases of ntpath.join: d already ends with a separator, and
    d is a bare drive letter ("C:").
    """
    if d[-1:] in ("/", "\\"):
        return d + n
    if len(d) == 2 and d[1] == ":":
        return d + n
    return d + os.sep + n


def _warm_build(root, skip_appdata, cache, stats):
    """Warm start: load -> mtime diff -> rescan changed directories -> merge."""
    stats["mode"] = "warm"
    t0 = time.perf_counter()
    rows = cache.load_rows()
    stats["load_ms"] = (time.perf_counter() - t0) * 1000

    seqs, mtimes, names = {}, {}, {}
    for p, seq, m, blob in rows:
        seqs[p], mtimes[p], names[p] = seq, m, decompress_names(blob)

    t0 = time.perf_counter()
    changed, deleted = [], []
    for p, m in mtimes.items():
        try:
            st = os.stat(p)
        except FileNotFoundError:
            deleted.append(p)
            continue
        except OSError:
            continue  # unreadable: trust the cache for now
        if st.st_mtime != m:
            changed.append((p, st.st_mtime))
    stats["diff_ms"] = (time.perf_counter() - t0) * 1000

    if deleted:
        dead = set()
        for d in deleted:
            dead.add(d)
            # Prefix matching must accept both separators: scandir's e.path keeps whatever
            # separator the caller passed in (with --walk C:/Users/... the whole tree uses
            # "/"), so os.sep alone would miss entries.
            for sep in ("/", "\\"):
                prefix = d + sep
                dead.update(k for k in mtimes if k.startswith(prefix))
        for d in dead:
            mtimes.pop(d, None)
            names.pop(d, None)
            seqs.pop(d, None)
        cache.delete_dirs(dead)

    next_seq = (max(seqs.values()) + 1) if seqs else 0
    for p, new_m in changed:
        try:
            new_names, new_rows = _rescan(p, mtimes, skip_appdata and p == root)
        except OSError:
            continue  # directory unreadable: keep the old rows and retry next time
        cache.upsert_dir(p, seqs[p], new_m, compress_names(new_names))
        mtimes[p], names[p] = new_m, new_names
        for sp, sm, snames in new_rows:
            cache.upsert_dir(sp, next_seq, sm, compress_names(snames))
            seqs[sp], mtimes[sp], names[sp] = next_seq, sm, snames
            next_seq += 1
        stats["rescanned"] += 1

    cache.commit()
    stats["entries"] = sum(len(v) for v in names.values())
    # Emitting in dict insertion order is exactly DFS visit order: load_rows uses ORDER BY
    # seq, rescanned directories get monotonically increasing seq values appended at the
    # tail, and pop() does not disturb the remaining order — insertion order and seq order
    # are the same invariant (earlier versions sorted again, which was pure overhead).
    files = []
    for d, ns in names.items():
        prefix = d if d[-1:] in ("/", "\\") or (len(d) == 2 and d[1] == ":") \
            else d + os.sep
        files.extend([prefix + n for n in ns])
    return files, stats
