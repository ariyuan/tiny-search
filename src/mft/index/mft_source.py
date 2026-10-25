#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mft_source.py — the $MFT index source (FSCTL_ENUM_USN_DATA) and its path resolution.

One traversal of the volume's USN journal produces a flat {frn: (parent_frn, name,
attrs)} table — names and the directory structure only, file contents are never
dereferenced. Paths are rebuilt lazily (path_of) or in one memoised pass
(materialize_paths) so a million-entry index never assembles paths up front.

Also owns the MFT read diagnostics (MFT_ERROR_HINTS): errno -> cause + suggested
action, written so a wrong control code is never misattributed to security software
(015's lesson).

Depends on: mft.platform.win32 (bindings + constants). Depended on by: mft.query.
"""

import ctypes
import struct
from ctypes import wintypes

from mft.platform.win32 import (ERROR_HANDLE_EOF, FSCTL_ENUM_USN_DATA, FRN_MASK,
                                GENERIC_READ, INVALID_HANDLE, MFT_ENUM_DATA_V0,
                                OPEN_EXISTING, FILE_SHARE_ALL, ROOT_FRN,
                                USN_RECORD_V2, USN_RECORD_V2_SIZE, k32)


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
            # The first 8 bytes of the output carry the next StartFileReferenceNumber.
            # That value is a raw 64-bit FRN (sequence number included) and must be
            # fed back un-masked: it is the driver's own cursor, not an identity.
            med.StartFileReferenceNumber = struct.unpack_from("<Q", raw, 0)[0]
            off = 8
            while off + USN_RECORD_V2_SIZE <= n:
                rec = struct.unpack_from(USN_RECORD_V2, raw, off)
                rec_len, frn, pfrn, attrs = rec[0], rec[3], rec[4], rec[10]
                name_len, name_off = rec[11], rec[12]
                if rec_len <= 0:
                    break
                name = raw[off + name_off: off + name_off + name_len].decode("utf-16-le", "replace")
                # Mask the sequence number off both the record and its parent
                # reference so every later equality test speaks the same identity
                # language (015 / FR-002).
                table[frn & FRN_MASK] = (pfrn & FRN_MASK, name, attrs)
                off += rec_len
    finally:
        k32.CloseHandle(h)
    return table


def path_of(table: dict, frn: int, volume: str) -> str:
    """Walk the frn -> parent chain and rebuild the path. Only called for hits, so millions
    of paths are never assembled up front.

    The root directory has no USN record of its own, so it never appears in the table:
    a top-level entry simply carries a parent reference to FRN 5 and the walk ends
    there. Both the "cur is the root" case and the "chain ended at the root" case
    therefore have to be handled to get the drive prefix right (015 / FR-003).
    """
    parts, seen, cur = [], set(), frn
    while cur in table and cur not in seen:
        seen.add(cur)
        pfrn, name, _ = table[cur]
        if cur == ROOT_FRN:       # the fixed FRN of the NTFS root directory
            parts.append(volume.rstrip(":") + ":")
            break
        if name:
            parts.append(name)
        if pfrn == cur:
            break
        cur = pfrn
    else:
        # The loop ended because cur is no longer in the table. If that is the root
        # directory, the chain simply stopped there — the drive prefix still applies.
        if cur == ROOT_FRN:
            parts.append(volume.rstrip(":") + ":")
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
        if cur == ROOT_FRN:          # top of the chain is the NTFS root: same rule as path_of
            base = volume.rstrip(":") + ":"
        elif cur in resolved:
            base = resolved[cur]
        else:                        # broken chain / cycle: no base, start from the own name
            base = ""
        for f in reversed(chain):    # build top-down, one pass
            if f == ROOT_FRN:
                resolved[f] = volume.rstrip(":") + ":"
                base = resolved[f]
                continue
            name = store[f][1]
            base = name if not base else base + "\\" + name
            resolved[f] = base
    return resolved


# ---------------------------------------------------------------- MFT diagnostics

MFT_ERROR_HINTS = {
    # errno 1 is ERROR_INVALID_FUNCTION: the file system does not support the control code.
    # It is NOT an access-denied error, so it says nothing about permissions — and it is
    # not evidence that a security product interfered. The most common cause by far is a
    # wrong FSCTL code: check that FSCTL_ENUM_USN_DATA is 0x000900B3 (function 0x2C).
    1: ("the file system rejected the FSCTL call (ERROR_INVALID_FUNCTION) — the control code is "
        "most likely wrong or unsupported by this volume. Check that FSCTL_ENUM_USN_DATA is "
        "0x000900B3 (CTL_CODE function 0x2C), and that the volume is NTFS. A security product "
        "blocking the call is a rarer possibility; the permission-free walk mode keeps working "
        "either way."),
    5: ("insufficient privileges (ERROR_ACCESS_DENIED). Whole-volume mode requires administrator "
        "rights: run as administrator, or use the permission-free walk mode."),
}


def _mft_error_hint(exc: OSError) -> str:
    """errno -> a human-readable cause and suggested action. Unknown errno gets a generic
    message plus the raw error code."""
    return MFT_ERROR_HINTS.get(
        exc.errno, "unknown cause (error code %s). Try the permission-free walk mode." % exc.errno)
