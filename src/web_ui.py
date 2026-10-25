#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
web_ui.py — the local web search UI of MFT Search (HTTP/window delivery facade).

018 架构重构后的角色：领域逻辑已沉入 `mft/` 包——范围模型在 `mft.scope`、默认页面
在 `mft.page`、进程 IO 计数器在 `mft.platform.win32`、索引入口在 `mft.index`、
缓存在 `mft.cache`（经 index_cache 门面）。本文件保留交付层：HTTP 路由与服务端
状态、索引构建编排、IO 速率计量、参数解析与 main()；pywebview 原生窗口载体在
`native_window.py`（本文件再导出，patch 面不变）。

这些编排函数留在本层的共同原因是**调用点必须在本模块命名空间解析**：测试打桩
`web_ui.is_admin` / `web_ui.build_index_cached` / `web_ui.build_mft_index` /
`web_ui.list_volumes` / `web_ui.process_io_bytes` / `web_ui.io_meter_of` /
`web_ui.ThreadingHTTPServer` / `web_ui.serve_in_thread` / `web_ui.run_window` 之后
跑 main() 或直接起 Handler，走的就是打桩函数（t007 / t013 / t016 / t017 / t019）。
原模块的公开名称（PAGE、scope_*、IoMeter、Handler……）经再导出一个不少。

--window mode is the native-window carrier backed by the optional pywebview dependency:
when it is missing or the WebView2 runtime is unavailable, the browser is used instead with
identical behaviour.

A loopback-only web UI wrapped around the CLI prototype (src/mft_finder.py):
  - builds the index once at startup (walk or mft) and keeps it in memory for all queries
  - opens http://127.0.0.1:8765 : search box + volume selector + search button + result list
  - in whole-volume MFT mode (administrator) the selector switches to another fixed NTFS
    volume: POST /api/switch rebuilds the index and swaps it atomically, GET /api/volumes
    lists the candidates (013). Other modes show the indexed folder instead and grey it out
  - matches file names only, never reads file contents
  - binds 127.0.0.1 only, the page makes no external requests, no telemetry of any kind

Usage:
    python src/web_ui.py                              # walk %USERPROFILE%, default port 8765
    python src/web_ui.py --walk C:\\Windows\\System32  # index one specific directory
    python src/web_ui.py --mft C                       # read the C: $MFT (needs an elevated shell)
    python src/web_ui.py --port 9000 --no-browser      # another port, do not open a browser
    python src/web_ui.py --window                      # pywebview native window (optional dep)

Search syntax matches the CLI exactly: space = AND, "!" prefix = exclude, case-insensitive.
The "Settings" dropdown switches the search scope (file name / full path): in full-path mode
an MFT index builds a path resolution table lazily on the first query and caches it; matched
keywords in the results are highlighted with <mark> (built through the DOM API, never
innerHTML).
"""

import argparse
import json
import os
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

# Same directory as this file, imported directly; index and search logic is never duplicated
from index_cache import build_index_cached, print_cache_line
from mft.errors import BuildCancelled
from mft.index.mft_source import _mft_error_hint, build_mft_index, materialize_paths
from mft.index.walk_source import is_user_dir
from mft.iometer import IoMeter as _IoMeter
from mft.page import PAGE
from mft.platform.volumes import is_admin, list_volumes, system_drive_root
from mft.platform.win32 import process_io_bytes
from mft.query import parse_query, run_search
from mft.scope import (is_drive_root, normalize_volume, scope_for_root, scope_label,
                       scope_prefix, volume_scope)

__all__ = [
    "BuildCancelled", "DEFAULT_PORT", "Handler", "IoMeter", "PAGE", "RESULT_LIMIT",
    "ThreadingHTTPServer", "build_index_cached", "build_mft_index", "build_state_of",
    "io_meter_of", "io_state_of", "is_admin", "is_drive_root", "is_user_dir",
    "list_volumes", "main", "materialize_paths", "normalize_volume", "parse_query",
    "port_in_use", "print_cache_line", "process_io_bytes", "request_scope", "resolve_scope",
    "run_search", "run_window", "scope_for_root", "scope_label", "scope_of", "scope_prefix",
    "serve_in_thread", "stop_server", "system_drive_root", "volume_scope",
]

RESULT_LIMIT = 200          # same default cap as run_search on the CLI
DEFAULT_PORT = 8765


# ---------------------------------------------------------------- server state helpers

def _public_stats(stats):
    """JSON-safe view of the cache stats.

    A cold build returns `stats["writer"]` — the handle of the background cache-writing
    thread, kept so one-shot CLI runs can join it before exiting. A Thread is not
    serialisable, and json.dumps fails *after* the response headers are on their way, so the
    client sees a dropped connection and the page stops refreshing. Filtering to scalars
    keeps /api/status alive no matter what the cache layer adds later.
    """
    if not stats:
        return None
    return {k: v for k, v in stats.items()
            if isinstance(v, (str, int, float, bool)) or v is None}


def build_state_of(httpd) -> dict:
    """The build state dict, created on demand: tests and embeddings that only inject
    index_store/index_meta keep working."""
    state = getattr(httpd, "build_state", None)
    if state is None:
        state = {"building": False, "progress": 0, "error": "", "note": "", "started": 0.0}
        httpd.build_state = state
    return state


def scope_of(httpd) -> dict:
    """The indexed scope; derived from index_meta when it was never set explicitly."""
    scope = getattr(httpd, "index_scope", None)
    if scope:
        return scope
    mode, root = httpd.index_meta[0], httpd.index_meta[1]
    return volume_scope(root) if mode == "mft" else scope_for_root(root)


# ---------------------------------------------------------------- disk I/O meter (017)

class IoMeter(_IoMeter):
    """Facade adapter over mft.iometer.IoMeter (018).

    The rate logic itself lives in mft.iometer.IoMeter; what stays here is the
    counter *binding*: sample() must resolve process_io_bytes through *this*
    module's namespace — that is the seam t019 patches (web_ui.process_io_bytes
    = fake, then web_ui.IoMeter(interval=1.0).sample()). The lambda captures no
    value at construction time, so the patched global is what gets called.
    """

    def __init__(self, interval: float = 1.0):
        super().__init__(interval, sampler=lambda: process_io_bytes())


def io_meter_of(httpd) -> IoMeter:
    """The per-server I/O meter, created on first use (tests and embeddings included)."""
    meter = getattr(httpd, "io_meter", None)
    if meter is None:
        meter = IoMeter()
        httpd.io_meter = meter
        meter.start()
    return meter


def io_state_of(httpd):
    """JSON-safe I/O view for /api/status: MB/s rates (1 MiB = 1048576 B) plus the
    session totals. Any failure degrades to None so one broken field can never take
    the whole status response down (the _public_stats defence, 016's lesson)."""
    try:
        return io_meter_of(httpd).snapshot()
    except Exception:
        return None


# ---------------------------------------------------------------- index builds (014)

def _publish(httpd, store, mode, root, scope, build_ms, cache_stats, note=""):
    """Swap in a finished index.

    The dead path table is cleared *before* the store is replaced so no request can pair the
    new index with the previous volume's paths (016 asserts this).
    """
    httpd.mft_paths = None
    httpd.mft_paths_cache = None
    httpd.index_store = (store, scope_prefix(scope, root))
    httpd.index_scope = scope
    httpd.index_meta = (mode, root, len(store), build_ms, False)
    httpd.index_cache = _public_stats(cache_stats) or {"mode": "mft"}
    httpd.build_state.update(building=False, progress=len(store), error="", started=0.0)
    if note:
        httpd.build_state["note"] = note


def _walk_worker(httpd, scope, gen):
    """Background walk: publishes the growing list as it goes, so searching works from the
    first second even on a 1.7M-entry system drive."""
    root = scope["root"]
    state = httpd.build_state

    def on_batch(files):
        if httpd.build_gen != gen:
            raise BuildCancelled()      # a newer scope request won, drop this build
        state["progress"] = len(files)
        httpd.index_store = (files, scope_prefix(scope, root))   # live list, no copy

    t0 = time.perf_counter()
    try:
        store, stats = build_index_cached(root, skip_appdata=is_user_dir(root),
                                          on_batch=on_batch)
    except BuildCancelled:
        sys.stderr.write("[build] cancelled: %s\n" % scope_label(scope))
        return
    except Exception as exc:            # never let a walk failure kill the server
        state["building"] = False
        state["error"] = "%s: %s" % (type(exc).__name__, exc)
        sys.stderr.write("[build] failed: %s (%s)\n" % (scope_label(scope), exc))
        return
    if httpd.build_gen != gen:
        return
    ms = (time.perf_counter() - t0) * 1000
    _publish(httpd, store, "walk", root, scope, ms, stats)
    sys.stderr.write("[build] %s: %s entries in %.1f s (%s)\n"
                     % (scope_label(scope), format(len(store), ","), ms / 1000,
                        stats.get("mode")))
    print_cache_line(stats)


def request_scope(httpd, scope, prefer_mft=True, force=False):
    """Index `scope` now. Returns (mode, cancelled_previous).

    Whole volumes try $MFT first (seconds) and fall back to a background walk; folders are
    always walked ($MFT is a whole-volume enumeration). Any in-flight build is cancelled by
    bumping the generation, which the old build observes at its next progress callback.
    """
    with httpd.build_lock:
        state_now = build_state_of(httpd)
        cancelled = bool(state_now.get("building"))
        if not force and not cancelled and scope_of(httpd) == scope:
            # Same scope: never burn a rebuild for a no-op selection (013 FR-006).
            return "unchanged", False
        httpd.build_gen += 1
        gen = httpd.build_gen
        state = build_state_of(httpd)
        state.update(building=True, progress=0, error="", note="", started=time.time())
        root = scope["root"]
        prefix = scope_prefix(scope, root)

        if prefer_mft and scope["kind"] == "volume" and is_admin():
            info = {v["letter"]: v for v in list_volumes()}.get(scope["target"])
            if info and info["selectable"]:
                print("[*] Reading $MFT of %s: ..." % scope["target"])
                t0 = time.perf_counter()
                try:
                    store = build_mft_index(scope["target"])
                except OSError as exc:
                    # Same wording as 007's build_index_verbose: cause + action, on stdout so
                    # a console user sees it, plus a short note that the page shows.
                    print("[!] Could not read $MFT of %s: (%s)" % (scope["target"], exc))
                    print("    Probable cause: %s" % _mft_error_hint(exc))
                    print("    Switched to permission-free mode automatically: indexing %s, "
                          "search still works." % root)
                    note = ("MFT unavailable for %s:, walking instead (%s)"
                            % (scope["target"], _mft_error_hint(exc).split(".")[0]))
                    state["note"] = note
                    httpd.index_store = ([], prefix)
                    httpd.index_scope = scope
                    httpd.index_meta = ("walk", root, 0, 0.0, False)
                    httpd.index_cache = None      # the old build's cold/warm label no longer applies
                    _spawn_walk(httpd, scope, gen)
                    return "pending", cancelled
                ms = (time.perf_counter() - t0) * 1000
                _publish(httpd, store, "mft", scope["target"], scope, ms, {"mode": "mft"})
                sys.stderr.write("[build] %s: %s entries in %.1f s (mft)\n"
                                 % (scope_label(scope), format(len(store), ","), ms / 1000))
                return "mft", cancelled

        # Walk path: publish an empty store immediately so the page's status is truthful
        # (nothing from the previous scope may leak into the new one).
        httpd.index_store = ([], prefix)
        httpd.index_scope = scope
        httpd.index_meta = ("walk", root, 0, 0.0, is_user_dir(root))
        httpd.index_cache = None      # the previous build's cold/warm label no longer applies
        httpd.mft_paths = None
        httpd.mft_paths_cache = None
        _spawn_walk(httpd, scope, gen)
        return "pending", cancelled


def _spawn_walk(httpd, scope, gen):
    t = threading.Thread(target=_walk_worker, args=(httpd, scope, gen), daemon=True)
    t.start()
    return t


class Handler(BaseHTTPRequestHandler):
    """Read-only / (the page), /api/search, /api/status, /api/volumes, plus the single
    state-changing route /api/switch (reindex another fixed NTFS volume). The index is
    injected by the server instance."""

    server_version = "MFTSearch/0.4"
    protocol_version = "HTTP/1.1"

    # ---- response helpers -----------------------------------------------
    def _send(self, body: bytes, content_type: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, code: int = 200):
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", code)

    # ---- routing ---------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/search":
            self._api_search()
        elif path == "/api/status":
            self._api_status()
        elif path == "/api/volumes":
            self._api_volumes()
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        """The only state-changing route is /api/switch (rebuild the index for another
        volume). Everything else stays read-only, so unknown POSTs are rejected."""
        path = self.path.split("?", 1)[0]
        # Drain the body first: HTTP/1.1 keep-alive would otherwise desynchronise the next
        # request on this connection if a caller ever sends one.
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 0:
                self.rfile.read(min(length, 1 << 20))
        except (TypeError, ValueError):
            pass
        if path == "/api/switch":
            self._api_switch()
        elif path == "/api/scope":
            self._api_scope()
        elif path in ("/", "/api/search", "/api/status", "/api/volumes"):
            self._send_json({"error": "method not allowed"}, 405)
        else:
            self._send_json({"error": "not found"}, 404)

    def _api_search(self):
        qs = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        query = (qs.get("q") or [""])[0]
        scope = (qs.get("scope") or ["filename"])[0]
        if scope not in ("filename", "path"):
            # No silent fallback: the scope switch must be honest, so bad values are reported.
            self._send_json({"error": "scope only supports filename / path, got %r" % scope}, 400)
            return
        store, volume = self.server.index_store
        paths = None
        if scope == "path" and isinstance(store, dict):
            paths = self._mft_paths(store, volume)
        # filename_only=True matches the file name only, not the rest of the path, and never
        # reads file contents; scope=path matches the full path instead.
        results, ms = run_search(store, query, volume,
                                 limit=RESULT_LIMIT, filename_only=(scope == "filename"),
                                 paths=paths)
        # terms = the AND keywords of this query (lower-cased, parse_query semantics) for the
        # front-end highlighter; exclusion terms (the "!" prefix) are deliberately absent —
        # highlighting a word that was excluded would be a visual lie.
        must, _deny = parse_query(query)
        self._send_json({"results": results, "count": len(results),
                         "ms": ms, "limit": RESULT_LIMIT,
                         "scope": scope, "terms": must,
                         # True while a walk is still growing: the page must say so rather
                         # than let a partial index look like the whole truth (014).
                         "partial": bool(build_state_of(self.server).get("building"))})

    def _mft_paths(self, store, volume):
        """The full-path resolution table for a dict index; built lazily on the first
        full-path query and cached on the server instance.

        Users who only search file names (the default scope) never pay for it; those who
        switch to full path pay one build on their first query (seconds for a million
        entries) and are then on par with a walk store.

        The cache is bound to the *store object* it was built from, not to the server: FRNs
        are only unique inside one volume, so after a volume switch (013) a table built from
        the old index would silently return "C:\\..." paths for D: records. An identity check
        makes reuse impossible across switches without any locking or invalidation ordering.
        """
        cache = getattr(self.server, "mft_paths_cache", None)
        if cache is not None and cache[0] is store:
            return cache[1]
        t0 = time.perf_counter()
        paths = materialize_paths(store, volume)
        self.server.mft_paths = paths              # kept for introspection / older tests
        self.server.mft_paths_cache = (store, paths)
        sys.stderr.write("[mft] Full-path table built: %s entries in %.2f s "
                         "(reused by later full-path queries)\n"
                         % (format(len(paths), ","), time.perf_counter() - t0))
        return paths

    def _api_status(self):
        mode, root, _entries, build_ms, skip_appdata = self.server.index_meta
        state = build_state_of(self.server)
        store = self.server.index_store[0]
        building = bool(state.get("building"))
        self._send_json({
            "mode": mode, "root": root,
            "entries": len(store),                 # live: grows while a walk is running
            "build_ms": build_ms, "skip_appdata": skip_appdata,
            "cache": _public_stats(getattr(self.server, "index_cache", None)),
            "scope": scope_of(self.server),
            "building": building,
            "progress": len(store),
            "building_ms": round((time.time() - state["started"]) * 1000, 1) if building else 0.0,
            "error": state.get("error", ""),
            "note": state.get("note", ""),
            # Live disk I/O of this process (017): MB/s rates plus session totals;
            # null when the counters are unavailable — never breaks the response.
            "io": io_state_of(self.server),
        })

    def _api_volumes(self):
        """Which fixed volumes this session can index, and how.

        Since 014 every fixed volume is switchable: with administrator rights the NTFS ones
        go through $MFT, everything else (and every volume without admin rights) is walked.
        The selector is therefore never disabled; `note` explains the degraded quality.
        """
        mode = self.server.index_meta[0]
        scope = scope_of(self.server)
        admin = is_admin()
        note = ""
        if not admin:
            note = ("Not running as administrator: drives are indexed by walking (slower, and "
                    "protected folders are skipped). Run as administrator for the $MFT index.")
        vols = []
        for v in list_volumes():
            item = dict(v)
            item["walkable"] = True                 # any fixed volume can be traversed
            item["current"] = (scope.get("kind") == "volume"
                               and scope.get("target") == v["letter"])
            vols.append(item)
        self._send_json({"volumes": vols, "scope": scope, "mode": mode,
                         "admin": admin, "can_switch": True, "note": note,
                         "entries": len(self.server.index_store[0])})

    def _api_scope(self):
        """Change what is indexed: a whole drive or one folder (014).

        `root` may be a drive ('D', 'D:////', 'D:/') or a directory path; a drive root becomes
        a volume scope, everything else a folder scope. Nothing is swapped on a bad path.
        """
        qs = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        raw = (qs.get("root") or qs.get("volume") or [""])[0].strip()
        if not raw:
            self._send_json({"error": "root is required (a drive letter or a folder path)"}, 400)
            return
        try:
            scope = scope_for_root(raw)
        except (OSError, ValueError) as exc:
            self._send_json({"error": "cannot use %r: %s" % (raw, exc)}, 400)
            return
        if scope["kind"] == "volume":
            known = {v["letter"] for v in list_volumes()}
            if scope["target"] not in known:
                self._send_json({"error": "%s: is not a fixed local volume" % scope["target"],
                                 "volume": scope["target"]}, 400)
                return
        elif not os.path.isdir(scope["target"]):
            self._send_json({"error": "not an existing folder: %s" % scope["target"]}, 400)
            return

        mode, cancelled = request_scope(self.server, scope)
        body = {"scope": scope, "mode": mode, "changed": mode != "unchanged",
                "cancelled_previous": cancelled,
                "entries": len(self.server.index_store[0]),
                "build_ms": self.server.index_meta[3]}
        if scope["kind"] == "volume":
            body["volume"] = scope["target"]
        self._send_json(body, 200)

    def _api_switch(self):
        """013 compatibility: switch to the whole of a volume. A thin alias for /api/scope."""
        qs = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        raw = (qs.get("volume") or [""])[0]
        volume = normalize_volume(raw)
        if volume is None:
            self._send_json({"error": "volume must be a single drive letter (A-Z), got %r"
                                      % raw}, 400)
            return
        self.path = "/api/scope?root=" + volume          # reuse the one validated code path
        self._api_scope()

    def log_message(self, fmt, *args):
        sys.stderr.write("[http] %s\n" % (fmt % args))


def port_in_use(port: int) -> bool:
    """Is something already listening on 127.0.0.1:port?

    Relying on bind() raising OSError is not enough: Windows SO_REUSEADDR (which socketserver
    enables by default) lets two sockets bind the same port and LISTEN at the same time, so a
    second instance would "succeed silently" and then steal new connections — exactly the
    silent failure this avoids. Probing with connect first is the most reliable check (this is
    a single-user local tool, the race between probe and bind is negligible).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def resolve_scope(args) -> dict:
    """Where to index (014). The default is the whole system drive, in every mode.

    - `--mft <letter>`: that whole volume, $MFT first (walk as the fallback)
    - `--walk <dir>`: that folder
    - nothing: the whole system drive

    `--mft` without administrator rights is not an error: it degrades to walking the same
    volume and says why. 009 still holds — no UAC prompt is ever raised here.
    """
    if args.mft:
        letter = normalize_volume(args.mft)
        if letter is None:
            print("[!] --mft expects a drive letter (e.g. --mft D), got %r; "
                  "using the system drive." % args.mft)
        elif not is_admin():
            print("[!] No administrator rights, cannot read $MFT of %s:." % letter)
            print("    Permission-free whole-drive mode: indexing %s instead."
                  % (letter + ":\\"))
            print("    For the $MFT index, right-click launch.bat and choose "
                  "\"Run as administrator\" (the tool never raises a UAC prompt).")
            # 014 FR-010: the requested volume still gets indexed, just by walking it. Falling
            # through to the system drive here would index a different drive than the one the
            # message above promises.
            return volume_scope(letter)
        else:
            return volume_scope(letter)
    if args.walk:
        return scope_for_root(args.walk)
    return scope_for_root(system_drive_root())


# ---------------------------------------------------------------- native window
#
# The pywebview carrier and the server lifecycle it needs live in native_window.py
# (the only module touching the optional dependency). They are imported here so the
# facade's patch surface stays intact: main() resolves serve_in_thread / run_window
# through *this* module's namespace, which is where tests replace them (t013).
from native_window import run_window, serve_in_thread, stop_server  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description="MFT Search local web UI (zero third-party deps by default; "
                    "--window needs the optional pywebview)")
    ap.add_argument("--walk", metavar="DIR",
                    help="walk the given directory to build the index (default %%USERPROFILE%%)")
    ap.add_argument("--mft", metavar="VOLUME",
                    help="read $MFT of the given volume to build the index "
                         "(requires an elevated shell)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="listening port (default %d)" % DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser")
    ap.add_argument("--window", action="store_true",
                    help="pywebview native window (needs pip install pywebview; falls back "
                         "to the browser when missing or the runtime is unavailable)")
    ap.add_argument("--skip-appdata", action="store_true",
                    help="walk mode: skip the top-level AppData directory "
                         "(machine-generated cache data)")
    args = ap.parse_args()

    if args.mft and args.walk:
        print("[x] --walk and --mft are mutually exclusive.")
        return 2

    # Probe the port before building the index: a busy port fails fast and readable,
    # instead of wasting a full traversal first.
    if port_in_use(args.port):
        print("[x] Port %d is already in use (something is listening on 127.0.0.1)." % args.port)
        print("    Retry with another port: --port %d" % (args.port + 1))
        return 1

    scope = resolve_scope(args)

    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        print("[x] Cannot bind port %d (%s)." % (args.port, e))
        print("    It may be taken; retry with another port: --port %d" % (args.port + 1))
        return 1

    # Build state exists before the first index request so /api/status is coherent from the
    # very first request (a walk keeps building while the page is already usable).
    httpd.index_store = ([], scope_prefix(scope, scope["root"]))
    httpd.index_scope = scope
    httpd.index_meta = ("walk", scope["root"], 0, 0.0, False)
    httpd.index_cache = {"mode": "mft"}
    httpd.mft_paths = None
    httpd.mft_paths_cache = None
    httpd.build_state = {"building": False, "progress": 0, "error": "", "note": "",
                         "started": 0.0}
    httpd.build_gen = 0
    httpd.build_lock = threading.Lock()

    print("[*] Index scope: %s" % scope_label(scope))
    # --walk is an explicit request for the permission-free path, so it never tries $MFT.
    mode, _cancelled = request_scope(httpd, scope, prefer_mft=not args.walk,
                                     force=True)
    if mode == "mft":
        print("[+] Index ready: %s entries in %.2f s"
              % (format(len(httpd.index_store[0]), ","), httpd.index_meta[3] / 1000))
    else:
        print("[*] Indexing in the background (no white screen): the page is usable now,")
        print("    results are marked partial until the walk finishes. Progress: /api/status")

    url = "http://127.0.0.1:%d/" % args.port
    print("[*] Server started: %s (loopback only, Ctrl-C to quit)" % url)

    # --window only swaps the "carrier": the service moves to a background thread and the
    # main thread is given to the GUI loop. Without --window none of the block below runs,
    # so the browser path stays byte-for-byte unchanged.
    server_thread = None
    if args.window:
        print("[*] Opening the native window (the first WebView2 start may take a few seconds) ...")
        server_thread = serve_in_thread(httpd)
        if run_window(url):
            stop_server(httpd, server_thread)
            print("[*] Window closed, service stopped")
            return 0
        # Optional dependency missing / runtime unavailable: the server thread keeps running,
        # and the browser path below takes over.

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            print("[!] Could not open a browser, please visit %s manually" % url)

    try:
        if server_thread is not None:
            # Fallback path: the service already runs on a background thread, the main
            # thread just waits (interruptible with Ctrl-C).
            while server_thread.is_alive():
                server_thread.join(timeout=0.5)
        else:
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Exited")
    finally:
        if server_thread is not None:
            stop_server(httpd, server_thread)
        else:
            httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
