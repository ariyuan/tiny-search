#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mft — the core library of MFT Search (index sources, cache, query, scope).

Layering (dependencies point downwards only):

    platform   Windows bindings + machine/volume facts        (L0, no library deps)
    index      $MFT source (admin) + walk source (fallback)   (L1: platform, errors)
    cache      SQLite persistence + warm-start diff           (L2: platform, index)
    query      query parsing + execution                      (L1': index only)
    scope      index scope model                              (leaf)
    page       default web page resource                      (leaf)
    iometer    rate meter over cumulative I/O counters        (leaf: platform, sampler injected)
    errors     cross-layer exception vocabulary               (leaf)

The three delivery adapters in the parent directory — mft_finder.py (CLI facade),
index_cache.py (cache service facade) and web_ui.py (HTTP/window facade) — import
from here and never the other way round: nothing under mft/ imports a facade.
"""

from mft.cache import (BUSY_TIMEOUT_MS, SCHEMA_VERSION, IndexCache, _discard,
                       _ensure_cache_dir, _join_fast, _rescan, cache_db_path,
                       compress_names, decompress_names, is_cacheable)
from mft.errors import BuildCancelled
from mft.index import (MFT_ERROR_HINTS, PROGRESS_INTERVAL, _mft_error_hint,
                       build_mft_index, build_walk_index, is_junction, is_user_dir,
                       materialize_paths, path_of, scan_dir, walk_with_dirs)
from mft.iometer import IoMeter
from mft.platform import (DRIVE_FIXED, ERROR_HANDLE_EOF, FILE_ATTRIBUTE_DIRECTORY,
                          FILE_SHARE_ALL, FRN_MASK, FSCTL_ENUM_USN_DATA, GENERIC_READ,
                          INVALID_HANDLE, MFT_ENUM_DATA_V0, OPEN_EXISTING, ROOT_FRN,
                          USN_RECORD_V2, USN_RECORD_V2_SIZE, is_admin, k32,
                          list_volumes, process_io_bytes, system_drive_root,
                          volume_info)
from mft.query import parse_query, run_search
from mft.scope import (is_drive_root, normalize_volume, scope_for_root, scope_label,
                       scope_prefix, volume_scope)
