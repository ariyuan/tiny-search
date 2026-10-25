#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""native_window.py — the pywebview native-window carrier and the server lifecycle it needs.

Separated from web_ui.py (018) because it is the only place the optional pywebview
dependency lives: when the package or the WebView2 runtime is missing, run_window
reports the cause and returns False so the caller falls back to the browser — the
"copy the folder and run" zero-dependency experience never breaks.

  - serve_in_thread: move serve_forever onto a daemon thread so the main thread is
    free for the GUI loop (Windows WebView2 requires the GUI message loop on the
    main thread, STA).
  - stop_server:     stop the HTTP service and reap that thread; shared by window
    close and Ctrl-C, so failures never raise a second exception on the way out.
  - run_window:      create the window and block in the GUI loop; True = closed
    normally.

The three functions are imported into web_ui's namespace, which keeps the facade's
patch surface intact (tests replace web_ui.serve_in_thread / web_ui.run_window and
main() resolves them there).
"""

import threading


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
