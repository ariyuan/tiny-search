#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
web_ui.py — the local web search UI of MFT Search (single file, zero third-party deps by default).

--window mode is the native-window carrier backed by the optional pywebview dependency:
when it is missing or the WebView2 runtime is unavailable, the browser is used instead with
identical behaviour.

A loopback-only web UI wrapped around the CLI prototype (src/mft_finder.py):
  - builds the index once at startup (walk or mft) and keeps it in memory for all queries
  - opens http://127.0.0.1:8765 : search box + search button + result list
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
from mft_finder import (build_index_verbose, is_admin, materialize_paths,
                        parse_query, run_search)

RESULT_LIMIT = 200          # same default cap as run_search on the CLI
DEFAULT_PORT = 8765

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MFT Search</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
         margin: 0; background: #f5f6f8; color: #1a1a1a; }
  .wrap { max-width: 860px; margin: 0 auto; padding: 32px 16px 64px; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  #status { font-size: 12px; color: #666; margin-bottom: 16px; }
  form { display: flex; gap: 8px; }
  #q { flex: 1; padding: 10px 12px; font-size: 15px;
       border: 1px solid #c8ccd4; border-radius: 8px; }
  #q:focus { outline: 2px solid #2563eb66; border-color: #2563eb; }
  button { padding: 10px 20px; font-size: 15px; border: 0; border-radius: 8px;
           background: #2563eb; color: #fff; cursor: pointer; }
  button:hover { background: #1d4ed8; }
  #meta { font-size: 13px; color: #444; margin: 14px 2px 8px; min-height: 18px; }
  #meta.hint { color: #b91c1c; }
  #results { list-style: none; margin: 0; padding: 0; }
  #results li { font-family: Consolas, "Cascadia Mono", monospace; font-size: 13px;
               padding: 6px 10px; background: #fff; border: 1px solid #e4e7ec;
               border-radius: 6px; margin-bottom: 6px; word-break: break-all; }
  /* Keyword highlight. <mark> is created through the DOM API; path data stays plain text. */
  #results li mark { background: #fde68a; color: #1a1a1a; border-radius: 2px;
                    padding: 0 1px; }
  /* Settings dropdown (search scope switch) */
  .settings { position: relative; }
  #settings-btn { background: #fff; color: #1a1a1a; border: 1px solid #c8ccd4;
                  white-space: nowrap; }
  #settings-btn:hover { background: #eef1f5; }
  #settings-menu { position: absolute; right: 0; top: calc(100% + 6px); z-index: 10;
                   background: #fff; border: 1px solid #e4e7ec; border-radius: 8px;
                   box-shadow: 0 4px 16px rgba(0,0,0,.12); padding: 10px 12px;
                   min-width: 150px; }
  #settings-menu[hidden] { display: none; }
  .settings-title { font-size: 12px; color: #666; margin: 0 0 6px; }
  #settings-menu label { display: flex; align-items: center; gap: 6px;
                         font-size: 13px; padding: 4px 0; cursor: pointer; }
</style>
</head>
<body>
<div class="wrap">
  <h1>MFT Search</h1>
  <div id="status">Loading index status...</div>
  <form id="form">
    <input id="q" type="text" placeholder="Search file names (space = AND, ! = exclude)"
           autocomplete="off" autofocus>
    <button type="submit">Search</button>
    <div class="settings">
      <button type="button" id="settings-btn">Settings</button>
      <div id="settings-menu" hidden>
        <div class="settings-title">Search scope</div>
        <label><input type="radio" name="scope" value="filename"> File name only</label>
        <label><input type="radio" name="scope" value="path"> Full path</label>
      </div>
    </div>
  </form>
  <div id="meta"></div>
  <ul id="results"></ul>
</div>
<script>
var form = document.getElementById('form');
var q = document.getElementById('q');
var meta = document.getElementById('meta');
var results = document.getElementById('results');
var settingsBtn = document.getElementById('settings-btn');
var settingsMenu = document.getElementById('settings-menu');
var scopeRadios = settingsMenu.querySelectorAll('input[name=scope]');

/* ---------- Search scope (settings dropdown + localStorage persistence) ---------- */
var SCOPE_KEY = 'mft-search.scope';
var SCOPE_LABEL = { filename: 'file name', path: 'full path' };
var scope = 'filename';
try { scope = localStorage.getItem(SCOPE_KEY) || 'filename'; } catch (e) {}
if (scope !== 'path') { scope = 'filename'; }   // fallback: anything but 'path' is the default

function syncScopeUI() {
  for (var i = 0; i < scopeRadios.length; i++) {
    scopeRadios[i].checked = (scopeRadios[i].value === scope);
  }
  q.placeholder = (scope === 'path' ? 'Search full paths' : 'Search file names') +
                  ' (space = AND, ! = exclude)';
}

settingsBtn.addEventListener('click', function (e) {
  e.stopPropagation();
  settingsMenu.hidden = !settingsMenu.hidden;
});
settingsMenu.addEventListener('click', function (e) { e.stopPropagation(); });
document.addEventListener('click', function () { settingsMenu.hidden = true; });
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') { settingsMenu.hidden = true; }
});
for (var i = 0; i < scopeRadios.length; i++) {
  scopeRadios[i].addEventListener('change', function () {
    scope = this.value;
    try { localStorage.setItem(SCOPE_KEY, scope); } catch (e) {}
    syncScopeUI();
    settingsMenu.hidden = true;
    if (q.value.trim()) { doSearch(); }   // switching scope re-runs the search immediately
  });
}

/* ---------- Keyword highlight (DOM API assembly, never innerHTML) ---------- */
/* Note: PAGE is a plain triple-quoted string, so backslashes in the regex are escaped
   again for the Python literal. */
function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
}

function highlight(text, terms) {
  var frag = document.createDocumentFragment();
  if (!terms || terms.length === 0) {
    frag.appendChild(document.createTextNode(text));
    return frag;
  }
  var re = new RegExp('(' + terms.map(escapeRegExp).join('|') + ')', 'gi');
  var last = 0, m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) {
      frag.appendChild(document.createTextNode(text.slice(last, m.index)));
    }
    var mark = document.createElement('mark');
    mark.textContent = m[0];               // textContent: path data never reaches the HTML parser
    frag.appendChild(mark);
    last = m.index + m[0].length;
    if (m[0].length === 0) { re.lastIndex++; }   // guard against zero-length matches looping forever
  }
  if (last < text.length) {
    frag.appendChild(document.createTextNode(text.slice(last)));
  }
  return frag;
}

/* ---------- Search ---------- */
function doSearch() {
  var query = q.value.trim();
  if (!query) {
    meta.textContent = 'Please enter a keyword';
    meta.className = 'hint';
    results.replaceChildren();
    return;
  }
  meta.textContent = 'Searching...';
  meta.className = '';
  fetch('/api/search?q=' + encodeURIComponent(query) + '&scope=' + scope)
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (d.error) {
        meta.textContent = d.error;
        meta.className = 'hint';
        results.replaceChildren();
        return;
      }
      var extra = d.count >= d.limit ? ' (limit ' + d.limit + ' reached, showing the first '
                                        + d.limit + ')' : '';
      meta.textContent = d.count + ' hits · ' + d.ms.toFixed(1) + ' ms · scope: ' +
        (SCOPE_LABEL[d.scope] || d.scope) + extra;
      meta.className = '';
      var frag = document.createDocumentFragment();
      for (var i = 0; i < d.results.length; i++) {
        var li = document.createElement('li');
        li.appendChild(highlight(d.results[i], d.terms));   // highlight + textContent, belt and braces
        frag.appendChild(li);
      }
      results.replaceChildren(frag);
    })
    .catch(function (err) {
      meta.textContent = 'Request failed: ' + err;
      meta.className = 'hint';
    });
}

form.addEventListener('submit', function (e) {
  e.preventDefault();
  doSearch();
});

fetch('/api/status').then(function (r) { return r.json(); }).then(function (s) {
  var cacheTxt = '';
  if (s.cache && s.cache.mode === 'warm') {
    cacheTxt = ' · cache warm start (' + s.cache.rescanned + ' directories rescanned)';
  } else if (s.cache && s.cache.mode === 'cold') {
    cacheTxt = ' · cache cold start (writing in the background)';
  } else if (s.cache && s.cache.mode === 'nocache') {
    cacheTxt = ' · cache disabled';
  }
  document.getElementById('status').textContent =
    'Index: ' + s.entries.toLocaleString() + ' entries · ' + s.mode +
    ' mode · built in ' + (s.build_ms / 1000).toFixed(2) + ' s' + cacheTxt +
    ' · scope: ' + SCOPE_LABEL[scope];
}).catch(function () {
  document.getElementById('status').textContent = 'Could not load index status';
});

syncScopeUI();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    """Read-only: / (the page), /api/search and /api/status. The index is injected by the
    server instance."""

    server_version = "MFTSearch/0.3"
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
                         "scope": scope, "terms": must})

    def _mft_paths(self, store, volume):
        """The full-path resolution table for a dict index; built lazily on the first
        full-path query and cached on the server instance.

        Users who only search file names (the default scope) never pay for it; those who
        switch to full path pay one build on their first query (seconds for a million
        entries) and are then on par with a walk store.
        """
        paths = getattr(self.server, "mft_paths", None)
        if paths is None:
            t0 = time.perf_counter()
            paths = materialize_paths(store, volume)
            self.server.mft_paths = paths
            sys.stderr.write("[mft] Full-path table built: %s entries in %.2f s "
                             "(reused by later full-path queries)\n"
                             % (format(len(paths), ","), time.perf_counter() - t0))
        return paths

    def _api_status(self):
        mode, root, entries, build_ms, skip_appdata = self.server.index_meta
        self._send_json({"mode": mode, "root": root, "entries": entries,
                         "build_ms": build_ms, "skip_appdata": skip_appdata,
                         "cache": getattr(self.server, "index_cache", None)})

    def log_message(self, fmt, *args):
        sys.stderr.write("[http] %s\n" % (fmt % args))


def port_in_use(port: int) -> bool:
    """Is something already listening on 127.0.0.1:port?

    Relying on bind() raising OSError is not enough: Windows SO_REUSEADDR (which socketserver
    enables by default) lets two sockets bind the same port and LISTEN at the same time, so a
    second instance would "succeed silently" and then steal new connections — exactly the
    silent failure this avoids. Probing with connect first is the most reliable check (this
    is a single-user local tool, the race between probe and bind is negligible).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def resolve_mode(args) -> tuple:
    """Return (mode, target). --mft without admin rights degrades to walk and says so;
    no UAC prompt is ever raised."""
    if args.mft:
        volume = args.mft.rstrip(":")
        if not is_admin():
            print("[!] No administrator rights, cannot read $MFT of %s:." % volume)
            print("    Switched to permission-free mode: indexing the user directory %s ."
                  % os.path.expanduser("~"))
            print("    For whole-volume mode, right-click launch.bat and choose "
                  "\"Run as administrator\" (the tool never raises a UAC prompt).")
            return "walk", os.path.expanduser("~")
        return "mft", volume
    if args.walk:
        return "walk", args.walk
    return "walk", os.path.expanduser("~")


# ---------------------------------------------------------------- native window

def serve_in_thread(httpd) -> threading.Thread:
    """Move serve_forever onto a daemon thread so the main thread is free for the GUI loop.

    Windows WebView2 requires the GUI message loop on the main thread (STA), so the HTTP
    service has to step aside. ThreadingHTTPServer is already thread-safe, no extra locking.
    """
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return t


def stop_server(httpd, thread: threading.Thread) -> None:
    """Stop the HTTP service and reap the background thread. Shared by window close and Ctrl-C.

    shutdown() must be called from a thread other than serve_forever; when that thread is
    already gone (never started, or finished) shutdown is skipped — BaseServer.shutdown()
    waits on the __is_shut_down event and blocks forever on a server that never served.
    Failures here never raise a second exception: the process is already on its way out and
    an error would only hide the real exit information.
    """
    try:
        if thread is not None and thread.is_alive():
            httpd.shutdown()
    except Exception:
        pass
    try:
        httpd.server_close()
    except Exception:
        pass
    if thread is not None:
        thread.join(timeout=2.0)


def run_window(url: str) -> bool:
    """pywebview native window mode. Returns True when the window closed normally.

    Optional dependency: webview is imported inside the function, and a missing package
    returns False so the caller falls back to the existing browser path — the "copy the
    folder and run" zero-dependency experience is preserved.

    Broad exception handling: pywebview throws different types depending on what is missing
    (ImportError / WebViewException / OSError are all possible). The goal is "no failure ever
    crashes the process": report the cause (including the original exception) and fall back.
    """
    try:
        import webview
    except ImportError:
        print("[!] pywebview is not installed, cannot use native window mode.")
        print("    Install it with: pip install pywebview")
        print("    Falling back to the browser.")
        return False
    try:
        webview.create_window("MFT Search", url, width=960, height=640)
        webview.start()
    except Exception as e:
        print("[!] Native window failed to start (%s)." % e)
        print("    Probable cause: the Edge WebView2 runtime is missing "
              "(shipped with Windows 10/11).")
        print("    Falling back to the browser.")
        return False
    return True


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

    mode, target = resolve_mode(args)

    # Build the index before serving: whenever the page opens, the index is guaranteed ready.
    fell_back = False
    cache_stats = None
    t0 = time.perf_counter()
    if mode == "mft":
        print("[*] Reading $MFT of %s: ..." % target)
        # A failed read (security software blocking the volume FSCTL, ...) must not crash:
        # report the cause and the suggested action, then degrade to walking the user
        # directory (with the slim rules).
        store, mode, target, notes, cache_stats = build_index_verbose(
            target, os.path.expanduser("~"), skip_appdata=True)
        fell_back = bool(notes)
        for line in notes:
            print(line)
        if fell_back:
            print_cache_line(cache_stats)
    else:
        lean = " (skipping top-level AppData)" if args.skip_appdata else ""
        print("[*] Walking %s%s ..." % (target, lean))
        store, cache_stats = build_index_cached(target, skip_appdata=args.skip_appdata)
        print_cache_line(cache_stats)
    build_ms = (time.perf_counter() - t0) * 1000
    print("[+] Index ready: %s entries in %.2f s"
          % (format(len(store), ","), build_ms / 1000))

    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        print("[x] Cannot bind port %d (%s)." % (args.port, e))
        print("    It may be taken; retry with another port: --port %d" % (args.port + 1))
        return 1

    httpd.index_store = (store, target if mode == "mft"
                         else (os.path.splitdrive(target)[0][:1] or "C"))
    # mode reflects the mode actually in effect (walk after a fallback); same for skip_appdata
    httpd.index_meta = (mode, target, len(store), build_ms,
                        (args.skip_appdata or fell_back) if mode == "walk" else False)
    # Cache statistics live in a separate attribute (the 5-tuple above stays untouched) and
    # are exposed through /api/status.
    httpd.index_cache = cache_stats or {"mode": "mft"}

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
