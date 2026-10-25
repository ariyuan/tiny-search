#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""location.py — where cache dbs live and which roots deserve one.

Policy lives here (not in platform, which only reports facts): cache only fixed
local NTFS volumes, because mtime semantics are unreliable on network shares,
removable drives and FAT32. The db name is derived from the root + the walk rules
so "same root, different rules" never share a file.

Depends on: mft.platform.volumes (facts). Depended on by: the cache service
facade (index_cache.py).
"""

import hashlib
import os
from pathlib import Path

from mft.platform.volumes import volume_info
from mft.platform.win32 import DRIVE_FIXED


def is_cacheable(root: str) -> bool:
    """Cache only fixed local NTFS volumes: mtime semantics are unreliable on network
    shares, removable drives and FAT32."""
    try:
        dtype, fs = volume_info(root)
    except OSError:
        return False
    return dtype == DRIVE_FIXED and (fs or "").upper() == "NTFS"


def cache_db_path(root: str, skip_appdata: bool, cache_dir: str = None) -> Path:
    """<cache_dir or project>/cache/index-<sha1(root|rules)[:12]>.db."""
    key = "%s|%s" % (os.path.abspath(root), "lean" if skip_appdata else "full")
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    # Default location: <project>/cache. This file is src/mft/cache/location.py, so the
    # project root is parents[3] (cache/ -> mft/ -> src/ -> project).
    base = Path(cache_dir) if cache_dir else (Path(__file__).resolve().parents[3] / "cache")
    return base / ("index-%s.db" % digest)


def _ensure_cache_dir(db_path: Path) -> bool:
    """Make sure the cache directory exists; returns False when it is read-only (no cache)."""
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return True
    except OSError:
        return False


def _discard(db_path: Path):
    """Delete an untrusted cache file (reversible: the next run rebuilds it)."""
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(str(db_path) + suffix).unlink()
        except OSError:
            pass
