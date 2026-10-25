#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""store.py — the cache's SQLite storage and its name-blob codec.

One row per directory (path, seq, mtime, names_blob). names_blob = a zlib-compressed
list of basenames (files + junctions, same semantics as the walk index). Measured:
286,190 paths -> 3.8 MB of compressed blocks, ~8 MB including 40k directory paths
(row-per-file storage would need 73 MB).

IndexCache is a thin wrapper around one cache db; the connection belongs to the
creating thread and is never shared across threads. The schema (dirs + meta,
WITHOUT ROWID) is a compatibility contract — t013 verifies that an old-schema db is
detected via the meta mismatch and rebuilt, which only works if the table shape
stays recognisable.

Depends on nothing else in the library. Depended on by: mft.cache.location (paths
of dbs), mft.cache.diff and the cache service facade (index_cache.py).
"""

import sqlite3
import zlib

SCHEMA_VERSION = "1"
BUSY_TIMEOUT_MS = 1000


def compress_names(names) -> bytes:
    """List of basenames -> zlib blob (level 6: 286k paths take 0.59 s -> 3.8 MB)."""
    return zlib.compress("\n".join(names).encode("utf-8"), 6)


def decompress_names(blob: bytes):
    """zlib blob -> list of basenames. Empty directory -> [].

    Note that `"".split("\\n") == ['']`, so the empty string needs a special case —
    otherwise an empty directory yields one ghost entry named "" (which becomes "dir\\"
    after joining) and a warm start invents files out of nowhere.
    """
    if not blob:
        return []
    text = zlib.decompress(blob).decode("utf-8")
    return text.split("\n") if text else []


class IndexCache:
    """Thin wrapper around one cache db. The connection belongs to the creating thread and
    is never shared across threads."""

    def __init__(self, db_path):
        self.path = db_path
        self.con = sqlite3.connect(str(db_path), timeout=BUSY_TIMEOUT_MS / 1000.0)
        try:
            self.con.execute("PRAGMA journal_mode=OFF")
            self.con.execute("PRAGMA synchronous=OFF")
        except sqlite3.Error:
            # A garbage file blows up during connect (journal_mode reads the db header).
            # At that point self.con exists but the constructor never finished, so the
            # caller has no object to close — clean up here, otherwise the handle stays
            # open on Windows and _discard cannot delete the file.
            self.close()
            raise

    def healthy(self) -> bool:
        """quick_check liveness probe; garbage or half-written files always count as corrupt."""
        try:
            row = self.con.execute("PRAGMA quick_check(1)").fetchone()
            return bool(row) and row[0] == "ok"
        except sqlite3.DatabaseError:
            return False

    def ensure_schema(self):
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS dirs ("
            " path TEXT PRIMARY KEY, seq INTEGER NOT NULL,"
            " mtime REAL NOT NULL, names BLOB NOT NULL) WITHOUT ROWID")
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")

    def read_meta(self) -> dict:
        try:
            return dict(self.con.execute("SELECT key, value FROM meta").fetchall())
        except sqlite3.DatabaseError:
            return {}

    def load_rows(self):
        """[(path, seq, mtime, names_blob)] ordered by seq (= DFS visit order of the cold build)."""
        return self.con.execute(
            "SELECT path, seq, mtime, names FROM dirs ORDER BY seq").fetchall()

    def upsert_dir(self, path: str, seq: int, mtime: float, names_blob: bytes):
        self.con.execute(
            "INSERT OR REPLACE INTO dirs (path, seq, mtime, names) VALUES (?,?,?,?)",
            (path, seq, mtime, names_blob))

    def delete_dirs(self, paths):
        self.con.executemany("DELETE FROM dirs WHERE path=?", [(p,) for p in paths])

    def replace_all(self, rows):
        """Full replacement in a single transaction. The caller wraps this in `with`.
        rows = [(path, seq, mtime, blob)]."""
        self.con.execute("DELETE FROM dirs")
        self.con.executemany(
            "INSERT INTO dirs (path, seq, mtime, names) VALUES (?,?,?,?)", rows)

    def write_meta(self, meta: dict):
        self.con.executemany(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
            [(k, str(v)) for k, v in meta.items()])

    def commit(self):
        self.con.commit()

    def close(self):
        try:
            self.con.close()
        except sqlite3.Error:
            pass
