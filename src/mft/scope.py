#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scope.py — the index scope model (what gets indexed, in a normalised shape).

A scope is {"kind": "volume"|"folder", "target": ..., "root": ...}: a whole drive
or one directory. Everything user-supplied ("d", "D:", " d:\\ ", "C:/Users/...")
funnels through scope_for_root, which absolutises and classifies once so every
later comparison is stable. A bare letter is a volume, never a relative path —
otherwise abspath("C") would invent "<cwd>\\C".

Leaf module: depends on nothing. Consumed by the web UI facade (and available to
any future delivery layer).
"""

import os


def normalize_volume(raw: str):
    """'d' / 'D:' / ' d ' -> 'D'. Anything that is not a single drive letter -> None."""
    v = (raw or "").strip().rstrip(":").strip().upper()
    return v if len(v) == 1 and "A" <= v <= "Z" else None


def volume_scope(letter: str) -> dict:
    """Whole-drive scope: the index covers every file on that volume."""
    return {"kind": "volume", "target": letter.upper(), "root": letter.upper() + ":\\"}


def is_drive_root(path: str) -> bool:
    """'D:', 'D:\\', 'D:/' -> True (a volume root, not a subdirectory)."""
    drive, tail = os.path.splitdrive(os.path.abspath(path))
    return bool(drive) and tail.strip("\\/") == ""


def scope_for_root(raw: str) -> dict:
    """Classify a user-supplied root: a drive root becomes a volume scope, anything else a
    folder scope. The path is absolutised here so every later comparison is stable.

    A bare letter ("C", "C:") is a volume, not a relative path — otherwise abspath("C")
    would turn it into "<cwd>\\C" (a folder that does not exist)."""
    text = (raw or "").strip()
    letter = normalize_volume(text)
    if letter is not None and (len(text) <= 2):
        return volume_scope(letter)
    path = os.path.abspath(os.path.expanduser(text))
    if is_drive_root(path):
        return volume_scope(path[0])
    return {"kind": "folder", "target": path, "root": path}


def scope_label(scope: dict) -> str:
    """Human form used by the page: 'whole drive C:' / 'folder D:\\Projects'."""
    if not scope:
        return "unknown"
    if scope.get("kind") == "volume":
        return "whole drive %s:" % scope["target"]
    return "folder %s" % scope["target"]


def scope_prefix(scope: dict, root: str) -> str:
    """Drive letter used to render MFT paths / status (a folder still lives on a drive)."""
    if scope.get("kind") == "volume":
        return scope["target"]
    return (os.path.splitdrive(root)[0][:1] or "C").upper()
