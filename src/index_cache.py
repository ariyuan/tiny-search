#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""index_cache.py — index persistence and incremental warm start (service facade).

018 架构重构后的角色：本文件是缓存服务的**门面 + 编排层**。存储、位置策略、增量
diff 已拆入 `mft/cache/`（store / location / diff），遍历内核在 `mft/index/
walk_source.py`；留在这里的是编排——build_index_cached 的冷启 / 温启 / 无缓存
三分支与后台写线程，原因是这些函数的调用点必须在**本模块命名空间**解析：
测试打桩 `ic.is_cacheable` / `ic.build_walk_index` / `ic.walk_with_dirs` 后跑
build_index_cached，走的就是打桩函数（t007）。

公开名称与原模块逐一对应（IndexCache / cache_db_path / is_cacheable / _discard /
build_walk_index / scan_dir / walk_with_dirs …），老调用方与测试零感知。

Architecture (moved into mft/cache/, behaviour unchanged):
  - Storage: SQLite (stdlib), one row per directory (path, seq, mtime, names_blob).
    names_blob = a zlib-compressed list of basenames (files + junctions, same semantics
    as build_walk_index). Measured: 286,190 paths -> 3.8 MB of compressed blocks, ~8 MB
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

import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mft.cache.diff import _join_fast, _rescan  # noqa: E402
from mft.cache.location import (_discard, _ensure_cache_dir, cache_db_path,  # noqa: E402
                                is_cacheable)
from mft.cache.store import (BUSY_TIMEOUT_MS, SCHEMA_VERSION, IndexCache,  # noqa: E402
                             compress_names, decompress_names)
from mft.index.walk_source import build_walk_index, scan_dir, walk_with_dirs  # noqa: E402

__all__ = [
    "BUSY_TIMEOUT_MS", "IndexCache", "SCHEMA_VERSION", "build_index_cached",
    "build_walk_index", "cache_db_path", "compress_names", "decompress_names",
    "is_cacheable", "print_cache_line", "scan_dir", "wait_writer", "walk_with_dirs",
]


# ---------------------------------------------------------------- public entry points

def build_index_cached(root: str, skip_appdata: bool = False,
                       cache_dir: str = None, background: bool = True, on_batch=None):
    """Unified index entry point: cache first, degrade on failure. The returned list has the
    same semantics as build_walk_index.

    Returns (files, stats). stats:
      mode      warm (cache diff) / cold (full rebuild) / nocache (no cache)
      load_ms / diff_ms / rescanned / entries / reason (for nocache)

    on_batch (014): optional progress hook forwarded to the traversal. It receives the live
    file list (called at most every PROGRESS_INTERVAL seconds) and may raise to abort the
    build — a cancelled cold build returns nothing and writes no cache, because the exception
    unwinds before the persist step.
    """
    stats = {"mode": "cold", "load_ms": 0.0, "diff_ms": 0.0,
             "rescanned": 0, "entries": 0, "reason": ""}

    def _no_cache(reason: str):
        files = build_walk_index(root, skip_appdata=skip_appdata, on_batch=on_batch)
        stats.update(mode="nocache", entries=len(files), reason=reason)
        if on_batch is not None:
            on_batch(files)                       # one final publish for the fast paths
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
            return _cold_build(root, skip_appdata, db_path, want_meta, stats, background, on_batch)
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
        return _cold_build(root, skip_appdata, db_path, want_meta, stats, background, on_batch)

    try:
        return _warm_build(root, skip_appdata, cache, stats, on_batch)
    except sqlite3.Error as exc:
        print("[cache] Warm start failed (%s), rebuilding from scratch." % exc)
        return _cold_build(root, skip_appdata, db_path, want_meta, stats, background, on_batch)
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


def _cold_build(root, skip_appdata, db_path, want_meta, stats, background, on_batch):
    """Cold start: a single walk returns the file list synchronously; writing happens on a
    background daemon thread.

    walk_with_dirs collects files and dir_rows in one traversal, so a cold start only
    touches the disk once and persistence reuses the same data (an early implementation ran
    build + persist separately and paid the IO twice).

    The thread handle is stored in stats["writer"] and callers should call wait_writer before
    the process exits — otherwise one-shot mode (--list) exits immediately, the daemon thread
    is cut short and the cache is never built.
    """
    files, dir_rows = walk_with_dirs(root, skip_appdata=skip_appdata, on_batch=on_batch)
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


def _warm_build(root, skip_appdata, cache, stats, on_batch=None):
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
    if on_batch is not None:
        on_batch(files)                            # warm start: one publish, no progress
    return files, stats
