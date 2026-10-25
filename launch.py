#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
launch.py — one-click launcher for MFT Search.

Usage:
    python launch.py                            # default: whole system drive in every mode
                                                #          (admin: $MFT; others: background walk)
    python launch.py --menu                     # interactive menu (five modes)
    python launch.py <dir>                      # start the web UI indexing <dir>
    python launch.py <dir> --port 9000          # port preference (default 8765, auto-avoids busy ports)
    python launch.py --mft C                    # whole-volume MFT web UI (needs an elevated shell,
                                                #          the tool never prompts for UAC itself)
    python launch.py <dir> --cli                # interactive CLI search
    python launch.py <dir> --list "kernel32"    # single query, then exit
    python launch.py <dir> --no-browser         # do not open a browser
    python launch.py --no-window                # force browser mode (skip the native window attempt)

You can also double-click launch.bat in the project root (it locates Python and hands over to here).

Behaviour:
  - Never requests UAC elevation: admins get the whole-volume $MFT index, everyone else gets
    a background walk of the whole system drive (the page is usable while it builds).
  - If reading $MFT fails (e.g. blocked by security software) it degrades to walk and says why.
  - Unexpected exceptions are written to logs/crash-<timestamp>.log.
  - The default entry point prefers a native window and falls back to the browser when
    pywebview is unavailable.
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import traceback
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
WEB_UI = SRC / "web_ui.py"
FINDER = SRC / "mft_finder.py"

sys.path.insert(0, str(SRC))
from mft_finder import (is_admin, is_user_dir,  # noqa: E402  reuse the library checks
                        system_drive_root)

DEFAULT_PORT = 8765
PORT_SCAN_RANGE = 20
PROBE_TIMEOUT = 0.5

MENU = """\
==============================================
   MFT Search launcher (--menu)
==============================================
   [1] Web search UI (default, no admin needed)
   [2] Interactive CLI search
   [3] Single CLI query
   [4] Whole-volume MFT web UI (needs admin: run as administrator, no auto-elevation)
   [5] Native window search UI (needs pip install pywebview, falls back to browser)
   [0] Quit
"""


# ---------------------------------------------------------------- basic interaction

def show_menu() -> str:
    """Print the menu and return the choice. EOF (exhausted piped input) returns "0"."""
    print(MENU)
    while True:
        try:
            choice = input("Select [0-5, Enter=1]> ").strip()
        except EOFError:
            print()
            return "0"
        if choice == "":
            return "1"
        if choice in ("0", "1", "2", "3", "4", "5"):
            return choice
        print("Invalid choice, please try again.")


def ask_directory(purpose: str) -> str:
    """Prompt for a directory; empty input means the user directory. Exits non-zero if missing."""
    default = os.path.expanduser("~")
    try:
        raw = input("Directory to index (%s) [Enter = %s]> " % (purpose, default)).strip()
    except EOFError:
        print()
        raw = ""
    return validate_directory(raw or default)


def validate_directory(directory: str) -> str:
    """The directory must exist: both the CLI argument and the menu prompt go through here."""
    if not os.path.isdir(directory):
        # Print the resolved absolute path: users often type a relative name (e.g. "test")
        # which is resolved against the cmd window's cwd, so echoing only the input hides
        # what was actually searched.
        print("[x] Directory does not exist: %s" % os.path.abspath(directory))
        sys.exit(2)
    return directory


def resolve_directory(arg_dir, purpose: str) -> str:
    """Use the argument when given (validated), otherwise prompt interactively."""
    return validate_directory(arg_dir) if arg_dir else ask_directory(purpose)


def spawn(cmd: list) -> int:
    """Spawn a child process with inherited stdio (so an interactive REPL works). Returns its code."""
    try:
        rc = subprocess.call(cmd)
    except KeyboardInterrupt:
        print("\n[*] Exited")
        return 0
    except FileNotFoundError as e:
        print("[x] Cannot start: %s" % e)
        return 1
    if rc != 0:
        # No pause here: launch.bat already handles it once (avoids a second "press any key").
        print("[x] The tool exited with code %d." % rc)
    return rc


# ---------------------------------------------------------------- port probing

def port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(PROBE_TIMEOUT)
        return s.connect_ex(("127.0.0.1", port)) == 0


def our_instance(port: int) -> bool:
    """Is the responder on this port our own web service (matched by the /api/status JSON shape)?

    Timeouts, non-JSON bodies and mismatched shapes all count as "some other program" —
    better to start one more service than to send the user's browser to a stranger's server.
    """
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:%d/api/status" % port, timeout=PROBE_TIMEOUT
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return isinstance(data, dict) and "mode" in data and "entries" in data
    except Exception:
        return False


def choose_port(preferred: int):
    """Return (port, already_running_here). Returns (None, False) when the whole range is taken."""
    for port in range(preferred, preferred + PORT_SCAN_RANGE):
        if not port_busy(port):
            return port, False
        if our_instance(port):
            return port, True
    return None, False


# ---------------------------------------------------------------- the three modes

def run_web(directory: str, port_pref: int, extra: list, open_browser: bool,
            mft_volume: str = None) -> int:
    port, existing = choose_port(port_pref)
    if port is None:
        print("[x] Ports %d-%d are all in use." % (port_pref, port_pref + PORT_SCAN_RANGE - 1))
        print("    Try another range: --port <port>")
        return 1

    if existing:
        url = "http://127.0.0.1:%d/" % port
        print("[*] The tool is already running on port %d; opening the browser instead." % port)
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:
                print("[!] Could not open a browser, please visit %s manually" % url)
        else:
            print("    Address: %s" % url)
        return 0

    if port != port_pref:
        print("[*] Port %d is taken by another program, using %d instead." % (port_pref, port))

    source = ("$MFT %s:" % mft_volume) if mft_volume else directory
    cmd = [sys.executable, str(WEB_UI)]
    if mft_volume:
        cmd += ["--mft", mft_volume]
    else:
        cmd += ["--walk", directory]
        if is_user_dir(directory):
            # Traversing the user directory (fallback or default) is slimmed automatically:
            # the top-level AppData folder is skipped.
            cmd += ["--skip-appdata"]
    cmd += ["--port", str(port)] + extra
    print("[*] Starting the web UI (%s, port %d) ..." % (source, port))
    return spawn(cmd)


def run_cli_interactive(directory: str, extra: list) -> int:
    print("[*] Starting interactive CLI mode (%s) ..." % directory)
    return spawn([sys.executable, str(FINDER), "--walk", directory] + extra)


def run_cli_once(directory: str, query: str, extra: list) -> int:
    print("[*] Querying (%s): %s" % (directory, query))
    return spawn([sys.executable, str(FINDER), "--walk", directory,
                  "--list", query] + extra)


def run_mft(volume: str, port_pref: int, extra: list, open_browser: bool) -> int:
    """Whole-volume MFT entry point (shared by --mft and menu item [4]).

    Never elevates on its own: without admin rights it states the limitation and degrades to
    walking the same volume (014: whole drive, not the user directory); with admin rights it
    goes the MFT route, and if the read is blocked the walk fallback explains why.
    """
    if not is_admin():
        print("[!] Administrator privileges not detected, cannot read $MFT of %s:." % volume)
        print("    Switched to permission-free mode: indexing the whole drive %s: (walk)."
              % volume)
        print("    For whole-volume mode, right-click launch.bat and choose \"Run as administrator\".")
        return run_web(volume + ":\\", port_pref, extra, open_browser)
    return run_web(None, port_pref, extra, open_browser, mft_volume=volume)


def run_default_mft(port_pref: int, extra: list, open_browser: bool,
                    window: bool = True) -> int:
    """The no-argument default path.

    Branches on the current privileges and never triggers a UAC prompt:
    - Administrator (the user chose "Run as administrator", or the shell already is one)
      -> read the C: $MFT; if security software blocks it, degrade to walk and explain.
    - Otherwise -> background walk of the whole system drive (014), with a notice covering
      mode, scope, first-run cost and how to get the $MFT index.

    The default entry point forwards --window: prefer the native window, and let web_ui's
    fallback chain drop to the browser when pywebview is missing (launch.py does not probe
    twice). window=False (--no-window) forces the browser and prints no fallback notice.
    """
    web_extra = list(extra)
    if window and "--window" not in web_extra:
        web_extra.append("--window")      # do not append it twice if the user passed it
    if is_admin():
        print("[*] Default mode: administrator privileges detected, reading the C: $MFT "
              "for a whole-volume index (seconds, main pipeline).")
        print("    (For permission-free mode or the CLI, add --menu)")
        return run_web(None, port_pref, web_extra, open_browser, mft_volume="C")

    drive_root = system_drive_root()
    print("[*] Default mode: no administrator privileges, using the permission-free walk "
          "mode (no UAC prompt).")
    print("    Scope: whole drive %s (the page is usable while the index builds in the"
          % drive_root)
    print("    background; the first cold walk of a full disk takes minutes, later starts")
    print("    warm-start from the cache).")
    print("    For the seconds-fast $MFT index, right-click launch.bat and choose")
    print("    \"Run as administrator\" (the tool never raises a UAC prompt on its own).")
    return run_web(drive_root, port_pref, web_extra, open_browser)


# ---------------------------------------------------------------- crash log

def write_crash_log() -> str | None:
    """Write the current thread's traceback to <project>/logs/crash-<timestamp>.log.

    Returns the absolute log path, or None when writing fails (cannot create the directory,
    read-only, ...). Never raises a second exception — the caller still prints the traceback
    to stderr and exits with code 1. Writes are confined to this project's own directory;
    no user file is ever touched.
    """
    try:
        logs = Path(__file__).resolve().parent / "logs"
        logs.mkdir(exist_ok=True)
        path = logs / ("crash-%s.log" % datetime.now().strftime("%Y%m%d-%H%M%S"))
        path.write_text(traceback.format_exc(), encoding="utf-8")
        return str(path)
    except OSError:
        return None


# ---------------------------------------------------------------- entry point

def run_menu(args, passthrough: list) -> int:
    """Explicit --menu entry point: five menu items, each branch unchanged."""
    choice = show_menu()
    if choice == "0":
        return 0
    if choice == "1":
        return run_web(ask_directory("web search"), args.port, passthrough, not args.no_browser)
    if choice == "2":
        return run_cli_interactive(ask_directory("interactive search"), passthrough)
    if choice == "3":
        directory = ask_directory("single query")
        try:
            query = input("Search keywords> ").strip()
        except EOFError:
            print()
            return 0
        if not query:
            print("[x] No keywords entered.")
            return 2
        return run_cli_once(directory, query, passthrough)
    if choice == "4":
        try:
            volume = input("Volume letter [Enter = C]> ").strip().upper()
        except EOFError:
            print()
            return 0
        return run_mft(volume or "C", args.port, passthrough, not args.no_browser)
    if choice == "5":
        # Native window = web UI + forwarded --window (pywebview is a soft dependency;
        # web_ui falls back to the browser when it is missing or the runtime is unavailable).
        return run_web(ask_directory("native window search"), args.port,
                       passthrough + ["--window"], not args.no_browser)
    return 0


def main() -> int:
    # Keep output line-visible when piped (automated tests) too, matching a double-click run.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    if not WEB_UI.exists() or not FINDER.exists():
        print("[x] Cannot find web_ui.py / mft_finder.py under src/.")
        print("    Keep launch.py and the src/ directory in the same project root "
              "(copying the whole folder is enough).")
        return 1

    ap = argparse.ArgumentParser(description="MFT Search one-click launcher")
    ap.add_argument("directory", nargs="?",
                    help="directory to index (omit for the default auto mode)")
    ap.add_argument("--menu", action="store_true", help="show the interactive menu (five modes)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="preferred port (default %d; busy ports are skipped)" % DEFAULT_PORT)
    ap.add_argument("--cli", action="store_true", help="interactive CLI mode")
    ap.add_argument("--list", dest="list_query", metavar="QUERY",
                    help="run a single query and exit")
    ap.add_argument("--mft", metavar="VOLUME",
                    help="whole-volume MFT web UI (requires administrator)")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser")
    ap.add_argument("--no-window", action="store_true",
                    help="force browser mode: the default entry point skips the pywebview "
                         "native window (and prints no hint when pywebview is missing)")
    args, extra = ap.parse_known_args()

    passthrough = list(extra)
    if args.no_browser:
        passthrough.append("--no-browser")

    # Explicit entries and mode flags win over the default path.
    if args.menu:
        return run_menu(args, passthrough)
    if args.mft:
        return run_mft(args.mft, args.port, passthrough, not args.no_browser)
    if args.list_query is not None:
        return run_cli_once(resolve_directory(args.directory, "single query"),
                            args.list_query, passthrough)
    if args.cli:
        return run_cli_interactive(resolve_directory(args.directory, "interactive search"),
                                   passthrough)
    if args.directory:
        return run_web(validate_directory(args.directory), args.port,
                       passthrough, not args.no_browser)

    # No arguments -> default auto start ("no-arg opens the menu" became the explicit --menu);
    # the default entry point forwards --window (turn it off with --no-window).
    return run_default_mft(args.port, passthrough, not args.no_browser,
                           window=not args.no_window)


def cli() -> int:
    """Process entry point: unexpected exceptions no longer crash silently.

    The traceback goes to logs/crash-<timestamp>.log and the path is echoed, exit code 1.
    SystemExit (including the exit code 2 from directory validation) is normal control flow
    and passes through untouched.
    """
    try:
        return main()
    except SystemExit:
        raise
    except BaseException:
        log_path = write_crash_log()
        traceback.print_exc()
        if log_path:
            print("[x] Unexpected error. Full log written to: %s" % log_path)
        return 1


if __name__ == "__main__":
    sys.exit(cli())
