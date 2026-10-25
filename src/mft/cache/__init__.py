#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cache — index persistence and incremental warm start (SQLite + zlib, stdlib only).

store: one db, one row per directory (path, seq, mtime, names_blob).
location: which roots get a cache and where the db lives.
diff: the warm-start kernel (rescan changed dirs, collect new subtrees).

The orchestration (cold / warm / no-cache decisions, background writer) lives in
the index_cache.py facade — it is the public entry point tests patch around.
Depends on platform (facts) and index (scan_dir).
"""

from mft.cache.diff import _join_fast, _rescan
from mft.cache.location import _discard, _ensure_cache_dir, cache_db_path, is_cacheable
from mft.cache.store import (BUSY_TIMEOUT_MS, SCHEMA_VERSION, IndexCache,
                             compress_names, decompress_names)
