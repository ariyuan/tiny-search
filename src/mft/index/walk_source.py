#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""walk_source.py — the permission-free walk index source (recursive os.scandir).

The single traversal kernel behind both public shapes: build_walk_index (a flat
list of full paths) and walk_with_dirs (the same list plus per-directory rows for
the cache). One traversal, two outputs — the two shapes stay item-for-item
identical because they share _walk (006 relies on that).

The progress hook (on_batch) is the only re-entry point into upper layers: it
receives the live file list at most every PROGRESS_INTERVAL seconds and may raise
to abort the walk — that is how the web UI cancels an in-flight build
(BuildCancelled, see mft.errors).

Depends on: mft.errors. Depended on by: mft.cache (diff + the cache service
facade) and mft_finder (compatibility entry points).
"""

import os
import time

from mft.errors import BuildCancelled

PROGRESS_INTERVAL = 0.5     # seconds between on_batch calls (014)


def is_junction(entry) -> bool:
    """Is this DirEntry a junction mount point (IO_REPARSE_TAG_MOUNT_POINT)?

    `is_junction()` is a Python 3.12+ DirEntry method reading the cached scandir attributes,
    so it costs no extra system call. On older versions it is treated as "not a junction".
    """
    fn = getattr(entry, "is_junction", None)
    return bool(fn()) if fn else False


def is_user_dir(path: str) -> bool:
    """Is path exactly the user directory? (samefile comparison; False on OSError).

    This is the "root == user directory -> skip top-level AppData" rule. It used to live in
    launch.py (is_profile_root); the CLI degradation path needs the same rule, so it moved
    into the library — same precedent as is_admin (no third copy).
    """
    try:
        return os.path.samefile(path, os.path.expanduser("~"))
    except OSError:
        return False


def scan_dir(path: str):
    """Scan a single directory level. Returns (names, subdirs):

    - names   = names of direct children that are "files + junctions". Junction mount points
                are not recursed into, only their name is recorded (same semantics as the
                old build_walk_index).
    - subdirs = full paths of real subdirectories to recurse into.
    Raises OSError when the directory itself is unreadable (the caller decides to skip);
    a failing per-entry check is skipped, as before.
    """
    names, subdirs = [], []
    with os.scandir(path) as it:
        for e in it:
            try:
                if e.is_dir(follow_symlinks=False):
                    if is_junction(e):
                        names.append(e.name)
                    else:
                        subdirs.append(e.path)
                else:
                    names.append(e.name)
            except OSError:
                pass
    return names, subdirs


def build_walk_index(root: str, max_files: int = 2_000_000, skip_appdata: bool = False,
                     on_batch=None, interval=None):
    """Permission-free fallback index via recursive os.scandir; returns a list of full paths.

    - Junction mount points (e.g. Local Settings / Application Data in the user directory,
      pointing inside AppData) are not recursed into, their path is only recorded —
      otherwise the same files would be traversed several times. This is the default.
    - skip_appdata=True skips the directory named AppData directly under the root (machine-
      generated cache data; an explicit --walk pointing at AppData itself is unaffected,
      the root itself is never skipped).
    - on_batch: optional progress hook, called with the *live* list at most every
      interval seconds (PROGRESS_INTERVAL by default). The list keeps growing after the
      call, so the receiver gets a partial view without any copying; raising from it
      aborts the walk.
    """
    return _walk(root, max_files, skip_appdata, False, on_batch, interval)[0]


def walk_with_dirs(root: str, max_files: int = 2_000_000, skip_appdata: bool = False,
                   on_batch=None, interval=None):
    """Traverse in the same order as build_walk_index and additionally emit directory rows
    (the data source for the cache).

    Returns (files, dir_rows):
    - files    identical to build_walk_index (same order).
    - dir_rows [(dir_path, mtime, names)] covering every directory in DFS order, empty ones
      included — an empty directory still needs a cache row so that files added later are
      detected by the mtime diff.
    """
    return _walk(root, max_files, skip_appdata, True, on_batch, interval)


def _walk(root, max_files, skip_appdata, collect_dirs, on_batch=None, interval=None):
    """One traversal, two shapes. The single traversal kernel keeps build_walk_index and
    walk_with_dirs item-for-item identical (006 relies on that).

    interval: seconds between on_batch calls; None reads this module's PROGRESS_INTERVAL.
    The mft_finder facade forwards its own (patchable) PROGRESS_INTERVAL here.
    """
    if interval is None:
        interval = PROGRESS_INTERVAL
    files, dir_rows = [], []
    stack = [(root, 0)]
    last = time.perf_counter()
    while stack and len(files) < max_files:
        d, depth = stack.pop()
        try:
            names, subdirs = scan_dir(d)
        except OSError:
            continue
        if depth == 0 and skip_appdata:
            subdirs = [s for s in subdirs if os.path.basename(s).lower() != "appdata"]
        files.extend(os.path.join(d, n) for n in names)
        if collect_dirs:
            try:
                dir_rows.append((d, os.stat(d).st_mtime, names))
            except OSError:
                pass
        stack.extend((s, depth + 1) for s in subdirs)
        if on_batch is not None:
            # Throttled on purpose: a callback per directory would add ~100k Python calls to
            # a 3-minute traversal for no visible gain.
            now = time.perf_counter()
            if now - last >= interval:
                last = now
                on_batch(files)
    return files, dir_rows


__all__ = ["BuildCancelled", "PROGRESS_INTERVAL", "build_walk_index", "is_junction",
           "is_user_dir", "scan_dir", "walk_with_dirs"]
