# MFT Search

A local file-name search tool for Windows. It reads the NTFS `$MFT` directly to build an in-memory index, so hundreds of thousands to millions of files can be queried in milliseconds. Without administrator rights it falls back to a plain directory walk, and everything keeps working.

- Two front ends: a **web UI** and a **native window**, plus a command-line mode
- Search syntax: space-separated terms are ANDed, a `!` prefix excludes
- Listens on `127.0.0.1` only, the page makes no external requests and collects no telemetry
- Zero third-party dependencies, only Python 3.13+ is required

## Requirements

| Item | Details |
|---|---|
| OS | Windows 10 / 11 (NTFS volumes) |
| Python | 3.13 or newer, with `py -3` or `python` on `PATH` |
| Optional | `pywebview` for the native window (otherwise the browser is used) |

## Quick start

Double-click **`launch.bat`** in the project root. It picks a mode based on your current privileges and **never raises a UAC prompt on its own**:

- **Not elevated** (the normal double-click case): indexes your user directory, skipping the top-level `AppData`. The first run takes a few seconds, later runs warm-start from the cache in 1–2 s.
- **Elevated** (right-click → *Run as administrator*): reads the `C:` `$MFT` and builds a whole-volume index in seconds.
- If `$MFT` is blocked by security software, it degrades to a directory walk, explains why, and does not crash.

The native window needs pywebview installed once. Note that `launch.bat` prefers `py -3` to find Python, so install into the same interpreter:

```bash
py -3 -m pip install pywebview
```

When pywebview is missing — or the system lacks the WebView2 runtime — it falls back to the browser and says so. No functionality is lost.

## Command line

```bash
# Default entry point: picks a mode from the current privileges (no UAC), native window first
python launch.py

# Interactive menu (web / CLI interactive / single query / whole-volume MFT / native window)
python launch.py --menu

# Force the browser for the default entry point, no native window attempt
python launch.py --no-window

# Web UI indexing a specific directory (no privileges needed)
python launch.py C:\Windows\System32

# Whole-volume index: requires an elevated shell, reads the C: $MFT
python launch.py --mft C

# Interactive CLI search / one-shot query
python launch.py C:\Windows\System32 --cli
python launch.py C:\Windows\System32 --list kernel32
```

You can also call the two modules under `src/` directly:

```bash
# Web UI (walks the user directory by default; --window uses the native window)
python src/web_ui.py --walk C:\Windows\System32
python src/web_ui.py --walk C:\Windows\System32 --window

# CLI prototype: without --list it enters interactive mode
python src/mft_finder.py --walk C:\Windows\System32 --list kernel32
python src/mft_finder.py --walk %USERPROFILE%
python src/mft_finder.py --mft C          # needs an elevated shell
```

### Options

`launch.py`

| Option | Description |
|---|---|
| `<dir>` | Start the web UI and index that directory |
| `--menu` | Show the interactive menu (web / CLI interactive / single query / whole-volume MFT / native window) |
| `--port N` | Preferred port, default 8765; a busy port is skipped |
| `--mft <volume>` | Whole-volume MFT web UI (run as administrator) |
| `--cli` | Interactive CLI mode |
| `--list "<query>"` | Run one query and exit |
| `--window` / `--no-window` | Force the native window / the browser for the default entry point |
| `--no-browser` | Do not open a browser |

`src/web_ui.py`

| Option | Description |
|---|---|
| `--walk <dir>` | Walk the given directory to build the index (default `%USERPROFILE%`) |
| `--mft <volume>` | Read the `$MFT` of the given volume (requires an elevated shell) |
| `--port N` | Listening port, default 8765, bound to `127.0.0.1` only |
| `--window` | pywebview native window |
| `--skip-appdata` | Walk mode: skip the top-level `AppData` directory |
| `--no-browser` | Do not open a browser |

`src/mft_finder.py`: `--walk <dir>`, `--mft <volume>`, `--list "<query>"`.

## Usage

### Search syntax

- Several space-separated terms are ANDed: all of them must match
- A `!` prefix excludes: paths containing that term are dropped
- Case-insensitive substring matching

Example: `report 2026 !tmp`

### Web UI

The **Settings** dropdown next to the search box switches the **search scope**:

- **File name only** (default): matches the file/directory name, fastest
- **Full path**: type a directory name such as `.workbuddy` to find every file below it

The scope is remembered in the browser's localStorage, so a refresh keeps it. Matched keywords are highlighted in yellow (excluded terms are not), and the result list is capped at 200 entries.

### Ports and existing instances

The default port is 8765; if it is taken, the next free port is used. If one of these instances is already running on that port, the browser is pointed at it instead of starting a second one.

## Notes

- **No automatic elevation**: for a whole-volume index, right-click `launch.bat` and choose *Run as administrator*
- When running elevated, UIPI applies: drag and drop from non-elevated windows does not work, and the elevated process cannot see network drives mapped in the normal session
- The fallback walk skips the top-level `AppData` (about 40% of it is machine cache). To search inside it, point the tool at it explicitly, e.g. `python src/mft_finder.py --walk %APPDATA%`
- MFT mode rebuilds its index on every start; walk mode persists a cache for fixed local NTFS volumes and warm-starts from it
- Security software may block `FSCTL_ENUM_USN_DATA` (measured with Microsoft PC Manager's filter driver). The tool then falls back to a directory walk and explains why; to use MFT, disable real-time protection temporarily and retry
- In MFT mode, switching to the full-path scope builds and keeps a path resolution table in memory (about 165 MB per million entries). The console reports how many entries were resolved and how long it took; switching back to *file name only* does not release it
- The web UI listens on `127.0.0.1` only and has no authentication, it is for local use
- `cache/` and `logs/` hold runtime artefacts (index cache, crash logs) and can be deleted at any time without affecting functionality

## Files

```text
launch.bat          # double-click entry point (pure ASCII bootstrap layer)
launch.py           # launcher: menu / port probing / spawning children
src/mft_finder.py   # index and search core + CLI
src/index_cache.py  # index cache (SQLite, compressed per directory)
src/web_ui.py       # local web UI (http.server + inline single page)
```

## License

[MIT](LICENSE)
