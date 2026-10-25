#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diff.py — the warm-start diff kernel (what changed since the cache was written).

_rescan: re-reads one directory and recursively collects rows for subtrees that are
not in the cache yet (a directory created while the tool was offline). Known
subdirectories keep their old row; their own mtime decides whether they get
rescanned, so no work is duplicated.

_join_fast: a character-for-character equivalent of os.path.join for a bare file
name, without join's split/checks (286k calls add up to ~200 ms, and the warm-start
budget is tight). Replicates ntpath.join's two special cases: a trailing separator
on the directory, and a bare drive letter ("C:").

Depends on: mft.index.walk_source (scan_dir). Depended on by: the cache service
facade (index_cache.py).
"""

import os

from mft.index.walk_source import scan_dir


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
