#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mft_finder.py — a minimal, teaching-oriented Everything clone (single file, zero dependencies).

Two index modes:
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

import ctypes
import os
import struct
import sys
import time
from ctypes import wintypes

GENERIC_READ = 0x80000000
FILE_SHARE_ALL = 0x00000007
OPEN_EXISTING = 3
FSCTL_ENUM_USN_DATA = 0x00090057
ERROR_HANDLE_EOF = 38
INVALID_HANDLE = 0xFFFFFFFFFFFFFFFF
FILE_ATTRIBUTE_DIRECTORY = 0x10

# USN_RECORD_V2 has a fixed 60-byte header followed by a UTF-16 file name.
USN_RECORD_V2 = "<IHHQQqQIIIIHH"
USN_RECORD_V2_SIZE = 60

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
k32.CreateFileW.restype = wintypes.HANDLE
k32.DeviceIoControl.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]
k32.DeviceIoControl.restype = wintypes.BOOL


class MFT_ENUM_DATA_V0(ctypes.Structure):
    _fields_ = [
        ("StartFileReferenceNumber", ctypes.c_ulonglong),
        ("LowUsn", ctypes.c_longlong),
        ("HighUsn", ctypes.c_longlong),
    ]


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ---------------------------------------------------------------- MFT index

def build_mft_index(volume: str) -> dict:
    """Return {frn: (parent_frn, name, attrs)}. Names and the directory structure only —
    file contents are never dereferenced."""
    path = "\\\\.\\%s:" % volume.rstrip(":")
    h = k32.CreateFileW(path, GENERIC_READ, FILE_SHARE_ALL, None, OPEN_EXISTING, 0, None)
    if h == INVALID_HANDLE or h is None:
        raise OSError(ctypes.get_last_error(),
                      "cannot open volume %s (usually means no administrator rights)" % path)

    table, buf = {}, ctypes.create_string_buffer(1 << 20)
    med = MFT_ENUM_DATA_V0(0, 0, 0x7FFFFFFFFFFFFFFF)
    returned = wintypes.DWORD(0)
    try:
        while True:
            ok = k32.DeviceIoControl(
                h, FSCTL_ENUM_USN_DATA, ctypes.byref(med), ctypes.sizeof(med),
                buf, ctypes.sizeof(buf), ctypes.byref(returned), None,
            )
            if not ok:
                if ctypes.get_last_error() == ERROR_HANDLE_EOF:
                    break
                raise OSError(ctypes.get_last_error(), "DeviceIoControl failed")
            n = returned.value
            if n < 8:
                break
            raw = buf.raw
            med.StartFileReferenceNumber = struct.unpack_from("<Q", raw, 0)[0]
            off = 8
            while off + USN_RECORD_V2_SIZE <= n:
                rec = struct.unpack_from(USN_RECORD_V2, raw, off)
                rec_len, frn, pfrn, attrs = rec[0], rec[3], rec[4], rec[10]
                name_len, name_off = rec[11], rec[12]
                if rec_len <= 0:
                    break
                name = raw[off + name_off: off + name_off + name_len].decode("utf-16-le", "replace")
                table[frn] = (pfrn, name, attrs)
                off += rec_len
    finally:
        k32.CloseHandle(h)
    return table


def path_of(table: dict, frn: int, volume: str) -> str:
    """Walk the frn -> parent chain and rebuild the path. Only called for hits, so millions
    of paths are never assembled up front."""
    parts, seen, cur = [], set(), frn
    while cur in table and cur not in seen:
        seen.add(cur)
        pfrn, name, _ = table[cur]
        if cur == 5:            # 5 is the fixed FRN of the NTFS root directory
            parts.append(volume.rstrip(":") + ":")
            break
        if name:
            parts.append(name)
        if pfrn == cur:
            break
        cur = pfrn
    return "\\".join(reversed(parts))


def materialize_paths(store: dict, volume: str) -> dict:
    """Resolve a dict index into {frn: full path} in one pass (the data source for path search).

    The result is identical to calling path_of on every record, but memoisation turns the
    O(parent chain depth) walk per record into O(1) amortised work: each directory path is
    resolved once and each child costs one string concatenation. A million-entry index is
    done in seconds. Built lazily (and cached) only when the web UI switches to the
    "full path" scope.

    Broken chains (missing parent record) or cycles (corrupt $MFT) degrade to the same
    behaviour as path_of: return the partial path resolved so far instead of raising.
    """
    resolved = {}
    for frn in store:
        if frn in resolved:
            continue
        chain, seen = [], set()
        cur = frn
        # Collect unresolved ancestors upwards; stop at an already-resolved node,
        # a broken chain or a cycle.
        while cur in store and cur not in resolved and cur not in seen:
            seen.add(cur)
            chain.append(cur)
            pfrn = store[cur][0]
            if pfrn == cur:          # self-reference: finish here, exactly like path_of
                break
            cur = pfrn
        if cur == 5:                 # top of the chain is the NTFS root: same rule as path_of
            base = volume.rstrip(":") + ":"
        elif cur in resolved:
            base = resolved[cur]
        else:                        # broken chain / cycle: no base, start from the own name
            base = ""
        for f in reversed(chain):    # build top-down, one pass
            if f == 5:
                resolved[f] = volume.rstrip(":") + ":"
                base = resolved[f]
                continue
            name = store[f][1]
            base = name if not base else base + "\\" + name
            resolved[f] = base
    return resolved


# ---------------------------------------------------------------- walk index

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


def build_walk_index(root: str, max_files: int = 2_000_000, skip_appdata: bool = False):
    """Permission-free fallback index via recursive os.scandir; returns a list of full paths.

    - Junction mount points (e.g. Local Settings / Application Data in the user directory,
      pointing inside AppData) are not recursed into, their path is only recorded —
      otherwise the same files would be traversed several times. This is the default.
    - skip_appdata=True skips the directory named AppData directly under the root (machine-
      generated cache data; an explicit --walk pointing at AppData itself is unaffected,
      the root itself is never skipped).
    - Implemented on top of the scan_dir primitive; the returned list matches the old
      implementation item for item.
    """
    files = []
    stack = [(root, 0)]
    while stack and len(files) < max_files:
        d, depth = stack.pop()
        try:
            names, subdirs = scan_dir(d)
        except OSError:
            continue
        if depth == 0 and skip_appdata:
            subdirs = [s for s in subdirs if os.path.basename(s).lower() != "appdata"]
        files.extend(os.path.join(d, n) for n in names)
        stack.extend((s, depth + 1) for s in subdirs)
    return files


def walk_with_dirs(root: str, max_files: int = 2_000_000, skip_appdata: bool = False):
    """Traverse in the same order as build_walk_index and additionally emit directory rows
    (the data source for the cache).

    Returns (files, dir_rows):
    - files    identical to build_walk_index (same order).
    - dir_rows [(dir_path, mtime, names)] covering every directory in DFS order, empty ones
      included — an empty directory still needs a cache row so that files added later are
      detected by the mtime diff.
    """
    files, dir_rows = [], []
    stack = [(root, 0)]
    while stack and len(files) < max_files:
        d, depth = stack.pop()
        try:
            names, subdirs = scan_dir(d)
        except OSError:
            continue
        if depth == 0 and skip_appdata:
            subdirs = [s for s in subdirs if os.path.basename(s).lower() != "appdata"]
        files.extend(os.path.join(d, n) for n in names)
        try:
            dir_rows.append((d, os.stat(d).st_mtime, names))
        except OSError:
            pass
        stack.extend((s, depth + 1) for s in subdirs)
    return files, dir_rows


# ---------------------------------------------------------------- unified index entry point

MFT_ERROR_HINTS = {
    1: ("the filter driver of a security product probably blocked the FSCTL call on the volume "
        "(ERROR_INVALID_FUNCTION). Disable real-time protection temporarily and retry, or verify "
        "on another machine; the permission-free walk mode keeps working here."),
    5: ("insufficient privileges (ERROR_ACCESS_DENIED). Whole-volume mode requires administrator "
        "rights: run as administrator, or use the permission-free walk mode."),
}


def _mft_error_hint(exc: OSError) -> str:
    """errno -> a human-readable cause and suggested action. Unknown errno gets a generic
    message plus the raw error code."""
    return MFT_ERROR_HINTS.get(
        exc.errno, "unknown cause (error code %s). Try the permission-free walk mode." % exc.errno)


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
        # Deferred import: index_cache depends on this module (build_walk_index etc.), so a
        # module-level import would be circular. Only the fallback path needs the cache,
        # which makes a function-local import the natural spot.
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


# ---------------------------------------------------------------- search

def parse_query(q: str):
    must, deny = [], []
    for t in q.split():
        if t.startswith("!") and len(t) > 1:
            deny.append(t[1:].lower())
        else:
            must.append(t.lower())
    return must, deny


def run_search(store, query: str, volume: str, limit: int = 200, filename_only: bool = False,
               paths: dict = None):
    """With filename_only=True a walk index only matches the basename (a dict index only ever
    matches record names anyway).

    Default False: list stores match the full path, dict stores match record names. The
    parameter exists for the "search file names only" option of the web UI.

    paths: the {frn: full path} table for dict stores (the output of materialize_paths).
    When given and filename_only=False, dict mode matches the full path, just like a list
    store; when omitted, behaviour stays "match record names only" and existing callers
    see no change.
    """
    must, deny = parse_query(query)
    if not must:
        # Neither an empty query nor an exclusion-only query (e.g. "!tmp") returns everything:
        # both yield an empty result and are still timed. The check must depend on `must`
        # alone — writing `not must and not deny` would miss the exclusion-only case and
        # wrongly return the whole index.
        return [], 0
    t0 = time.perf_counter()
    hits, results, scanned = [], [], 0
    if isinstance(store, dict):
        match_path = paths is not None and not filename_only
        for frn, (_, name, _) in store.items():
            scanned += 1
            low = (paths[frn] if match_path else name).lower()
            if all(m in low for m in must) and not any(d in low for d in deny):
                hits.append(frn)
                if len(hits) >= limit * 20:
                    break
        # In path mode the resolution table already holds the paths, so no path_of walk needed.
        results = [(paths[f] if match_path else path_of(store, f, volume))
                   for f in hits[:limit]]
    else:
        for p in store:
            scanned += 1
            if filename_only:
                # Take the trailing file name only. Hand-sliced with rfind instead of
                # os.path.basename: ntpath.basename goes through split() internally and
                # measured an order of magnitude slower on million-entry queries.
                i, j = p.rfind("\\"), p.rfind("/")
                low = p[(i if i > j else j) + 1:].lower()
            else:
                low = p.lower()
            if all(m in low for m in must) and not any(d in low for d in deny):
                results.append(p)
                if len(results) >= limit:
                    break
    return results, (time.perf_counter() - t0) * 1000


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
