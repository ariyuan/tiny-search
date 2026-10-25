#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""volumes.py — what this machine and its volumes are (read-only facts).

Answers three questions and nothing else:
  - is_admin(): is this process elevated? (decides whether $MFT is reachable)
  - list_volumes(): which local fixed volumes exist, and can each be indexed via $MFT?
  - system_drive_root(): the drive Windows is installed on ("C:\\")
  - volume_info(path): (drive_type, fs_name) for the volume holding a path

Facts only — no policy. "May I cache this volume" is the cache layer's decision
(cache.location.is_cacheable consumes these facts); "which scope do we index" is
the caller's decision. All calls are read-only metadata: no traversal, no $MFT
read, no user file touched.
"""

import ctypes
import os

from mft.platform.win32 import DRIVE_FIXED, k32


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def list_volumes() -> list:
    """Local fixed volumes and whether each one can be indexed through $MFT.

    Read-only metadata only (`GetLogicalDrives` / `GetDriveTypeW` /
    `GetVolumeInformationW`): no traversal, no $MFT read, no user file touched.

    Returns [{"letter", "label", "fs", "selectable", "reason"}] sorted by letter.
    - letter: uppercase single letter, no colon ("C")
    - selectable: True only for NTFS (FAT32/exFAT have no $MFT to enumerate)
    - reason: why it is not selectable ("" when it is)

    Removable, network and CD-ROM drives are deliberately absent: "whole volume"
    here means a fixed local disk (an unplugged USB stick would leave a stale
    index behind), see spec 013 assumptions.
    """
    try:
        mask = k32.GetLogicalDrives()
    except Exception:      # never let volume enumeration break the UI
        return []
    out = []
    for i in range(26):
        if not (mask >> i) & 1:
            continue
        letter = chr(ord("A") + i)
        root = letter + ":\\"
        try:
            if k32.GetDriveTypeW(root) != DRIVE_FIXED:
                continue
            label_buf = ctypes.create_unicode_buffer(261)
            fs_buf = ctypes.create_unicode_buffer(64)
            ok = k32.GetVolumeInformationW(
                root, label_buf, len(label_buf), None, None, None, fs_buf, len(fs_buf))
        except Exception:  # a volume that disappears between the two calls
            continue
        label = label_buf.value if ok else ""
        fs = fs_buf.value if ok else ""
        selectable = fs.upper() == "NTFS"
        out.append({
            "letter": letter,
            "label": label,
            "fs": fs,
            "selectable": selectable,
            "reason": "" if selectable else (fs or "unknown file system"),
        })
    return out


def system_drive_root() -> str:
    """The root of the drive Windows is installed on ("C:\\"), with a trailing separator.

    Used as the default scope since 014: the whole system drive, not the user directory.
    Reads %SystemDrive% (falls back to the drive of %SystemRoot%, then "C:") so a machine
    with Windows on D: is not silently narrowed to C:.
    """
    drive = os.environ.get("SystemDrive") or os.path.splitdrive(
        os.environ.get("SystemRoot", ""))[0]
    drive = (drive or "C:").rstrip("\\/")
    if len(drive) == 1:
        drive += ":"
    return drive + "\\"


def volume_info(path: str):
    """(drive_type, fs_name). Returns (None, None) for UNC paths and errors."""
    root = os.path.splitdrive(os.path.abspath(path))[0]
    if not root:
        return None, None
    drive = root + "\\"
    dtype = k32.GetDriveTypeW(drive)
    fs = ctypes.create_unicode_buffer(32)
    ok = k32.GetVolumeInformationW(drive, None, 0, None, None, None, fs, 32)
    return dtype, (fs.value if ok else None)
