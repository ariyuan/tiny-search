#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mft_finder.py — index & search entry points plus the CLI (compatibility facade).

018 架构重构后的角色：实现已按分层拆入 `mft/` 包——platform（Windows 绑定与卷事实）、
index（$MFT 读取 / 目录遍历）、query（查询）、scope（范围模型）。本文件保留三件事：

  1) **兼容再导出**：原模块的公开名称一个不少（常量、结构体、函数），
     `from mft_finder import build_walk_index` / `mf.FSCTL_ENUM_USN_DATA` 照旧可用；
  2) **统一索引入口** build_index / build_index_verbose：$MFT 优先、walk 兜底。
     留在这层是因为调用点必须在本模块命名空间解析 `build_mft_index`——测试打桩
     `mf.build_mft_index` 后调 `mf.build_index`，降级文案走的就是打桩函数（t007）；
  3) **CLI main()**：调用点解析 `is_admin`，并在调用时才 import 缓存服务
     （t009 打桩 `ic.build_index_cached` 后跑 main 的缝；纯 MFT 路径也不加载缓存层）。

两个索引模式（实现见 mft.index）：
  1) mft mode:  reads the NTFS $MFT directly (FSCTL_ENUM_USN_DATA); hundreds of thousands to
                millions of files indexed in seconds. Requires administrator rights. This is
                the real reason Everything is fast. If the read fails (e.g. security software
                blocking the volume FSCTL) it does not crash: it degrades to walk mode and
                prints the cause and the suggested action (see build_index).
  2) walk mode: recursive traversal with os.scandir. No privileges needed, an order of
                magnitude slower.

Usage:
    python mft_finder.py                    # pick a mode automatically, then interactive search
    python mft_finder.py --walk C:\\Users   # force a walk of the given directory
    python mft_finder.py --mft C            # force MFT mode on volume C:
    python mft_finder.py --list "report"    # one-shot query, no interactive loop

Search syntax:
    space-separated terms = AND,  "!" prefix = exclude,  case-insensitive substring matching
    e.g.  "report 2026 !tmp"
"""

import os
import sys
import time

from mft.errors import BuildCancelled
from mft.index.mft_source import (MFT_ERROR_HINTS, _mft_error_hint, build_mft_index,
                                  materialize_paths, path_of)
from mft.index.walk_source import (build_walk_index as _lib_build_walk_index,
                                   is_junction, is_user_dir, scan_dir,
                                   walk_with_dirs as _lib_walk_with_dirs)
from mft.platform.win32 import (DRIVE_FIXED, ERROR_HANDLE_EOF, FILE_ATTRIBUTE_DIRECTORY,
                                FILE_SHARE_ALL, FRN_MASK, FSCTL_ENUM_USN_DATA,
                                GENERIC_READ, INVALID_HANDLE, MFT_ENUM_DATA_V0,
                                OPEN_EXISTING, ROOT_FRN, USN_RECORD_V2,
                                USN_RECORD_V2_SIZE, k32)
from mft.platform.volumes import is_admin, list_volumes, system_drive_root
from mft.query import parse_query, run_search

__all__ = [
    "BuildCancelled", "DRIVE_FIXED", "ERROR_HANDLE_EOF", "FILE_ATTRIBUTE_DIRECTORY",
    "FILE_SHARE_ALL", "FRN_MASK", "FSCTL_ENUM_USN_DATA", "GENERIC_READ", "INVALID_HANDLE",
    "MFT_ENUM_DATA_V0", "MFT_ERROR_HINTS", "OPEN_EXISTING", "PROGRESS_INTERVAL", "ROOT_FRN",
    "USN_RECORD_V2", "USN_RECORD_V2_SIZE", "_mft_error_hint", "build_index",
    "build_index_verbose", "build_mft_index", "build_walk_index", "is_admin", "is_junction",
    "is_user_dir", "k32", "list_volumes", "main", "materialize_paths", "parse_query",
    "path_of", "run_search", "scan_dir", "system_drive_root", "walk_with_dirs",
]


# ---------------------------------------------------------------- walk entry points
#
# 遍历内核住在 mft.index.walk_source；这两个入口留在本模块，是为了维持公开面上
# 的节流旋钮：测试打桩 mft_finder.PROGRESS_INTERVAL 后调用 mft_finder.walk_with_dirs，
# 看到的就是打桩值（t017 S6 的节流契约）。门面把本模块的值显式传进库层，
# 库层自身仍是自洽的默认值（interval=None 时读它自己的 PROGRESS_INTERVAL）。

PROGRESS_INTERVAL = 0.5     # seconds between on_batch calls (014)


def build_walk_index(root: str, max_files: int = 2_000_000, skip_appdata: bool = False,
                     on_batch=None):
    """Permission-free fallback index via recursive os.scandir; returns a list of full paths.

    Facade entry: forwards this module's PROGRESS_INTERVAL to the traversal kernel.
    See mft.index.walk_source.build_walk_index for the full contract.
    """
    return _lib_build_walk_index(root, max_files, skip_appdata, on_batch,
                                 interval=PROGRESS_INTERVAL)


def walk_with_dirs(root: str, max_files: int = 2_000_000, skip_appdata: bool = False,
                   on_batch=None):
    """Traverse in the same order as build_walk_index and additionally emit directory rows
    (the data source for the cache). Returns (files, dir_rows).

    Facade entry: forwards this module's PROGRESS_INTERVAL to the traversal kernel.
    See mft.index.walk_source.walk_with_dirs for the full contract.
    """
    return _lib_walk_with_dirs(root, max_files, skip_appdata, on_batch,
                               interval=PROGRESS_INTERVAL)


# ---------------------------------------------------------------- unified index entry point

def build_index_verbose(volume: str, walk_root: str, skip_appdata: bool = False,
                        background: bool = True):
    """build_index plus cache statistics.

    Returns (store, mode, target, notes, cache_stats):
    - MFT success:  cache_stats = {"mode": "mft"} (no cache involved)
    - walk fallback: cache_stats comes from build_index_cached (warm/cold/nocache)
    """
    try:
        return build_mft_index(volume), "mft", volume, [], {"mode": "mft"}
    except OSError as exc:
        notes = [
            "[!] Could not read $MFT of %s: (%s)" % (volume, exc),
            "    Probable cause: %s" % _mft_error_hint(exc),
            "    Switched to permission-free mode automatically: indexing %s, "
            "search still works." % walk_root,
        ]
        # Deferred import on purpose (018): the mft_finder <-> index_cache cycle is gone
        # (the traversal kernel now lives in mft.index.walk_source), but the cache layer is
        # still resolved at call time — the MFT-only path never loads sqlite, and the CLI
        # resolves it lazily so tests can stub ic.build_index_cached before main() runs (t009).
        from index_cache import build_index_cached
        files, stats = build_index_cached(walk_root, skip_appdata=skip_appdata,
                                          background=background)
        return files, "walk", walk_root, notes, stats


def build_index(volume: str, walk_root: str, skip_appdata: bool = False):
    """Unified index entry point that prefers MFT and falls back to walk when $MFT fails.

    Returns (store, mode, target, notes):
    - success: mode="mft", target=volume, notes=[] (empty list, no extra output)
    - failure: mode="walk", target=walk_root, notes=diagnostic lines (cause and action)

    The fallback happens once and is never retried — a failing DeviceIoControl costs
    milliseconds. The signature is unchanged; the fallback path goes through the cache
    entry point (see build_index_verbose).
    """
    return build_index_verbose(volume, walk_root, skip_appdata)[:4]


# ---------------------------------------------------------------- CLI

def main():
    argv = sys.argv[1:]
    mode, target, once = None, None, None

    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--mft":
            mode, target = "mft", argv[i + 1].rstrip(":"); i += 2
        elif a == "--walk":
            mode, target = "walk", argv[i + 1]; i += 2
        elif a == "--list":
            once = argv[i + 1]; i += 2
        else:
            i += 1

    volume = "C"
    degraded = False          # whether we are on the automatic fallback path (keeps slim rules)
    if mode == "mft":
        # Never requests UAC. Without administrator rights: state the limitation and fall
        # back to walking the user directory (consistent with launch.py and web_ui).
        if not is_admin():
            print("[!] No administrator rights, cannot read $MFT of %s:." % target)
            print("    Switched to permission-free mode: indexing the user directory %s ."
                  % os.path.expanduser("~"))
            print("    For whole-volume mode, run this program as administrator "
                  "(the tool never raises a UAC prompt on its own).")
            mode, target, degraded = "walk", os.path.expanduser("~"), True
    elif mode == "walk":
        volume = os.path.splitdrive(target)[0][:1] or "C"

    if mode == "mft":
        print("[*] Reading $MFT of %s: ..." % target)
        t0 = time.perf_counter()
        store, mode, target, notes, cache_stats = build_index_verbose(
            target, os.path.expanduser("~"), skip_appdata=True)
        for line in notes:
            print(line)
        print("[+] Index ready: %s entries in %.2f s"
              % (format(len(store), ","), time.perf_counter() - t0))
        if mode == "walk":
            from index_cache import print_cache_line
            print_cache_line(cache_stats)
    else:
        root = target or os.path.expanduser("~")
        # The automatic fallback path keeps the slim rules (root == user directory ->
        # skip top-level AppData); an explicit --walk is never slimmed (explicit intent wins).
        slim = degraded and is_user_dir(root)
        print("[*] Walking %s%s ..." % (root, " (skipping top-level AppData)" if slim else ""))
        t0 = time.perf_counter()
        from index_cache import build_index_cached, print_cache_line
        store, cache_stats = build_index_cached(root, skip_appdata=slim)
        print("[+] Index ready: %s files in %.2f s"
              % (format(len(store), ","), time.perf_counter() - t0))
        print_cache_line(cache_stats)

    def show(q):
        res, ms = run_search(store, q, volume)
        print("  -> %d hits, search took %.1f ms" % (len(res), ms))
        for p in res:
            print("    " + p)

    if once is not None:
        show(once)
        from index_cache import wait_writer
        wait_writer(cache_stats)   # let the cache finish writing before the process exits
        return

    print("Type keywords to search (space = AND, ! prefix = exclude), empty line quits")
    while True:
        try:
            q = input("search> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q:
            break
        show(q)

    from index_cache import wait_writer
    wait_writer(cache_stats)


if __name__ == "__main__":
    main()
