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

- **Not elevated** (the normal double-click case): indexes the **whole system drive** with a directory walk. The window opens immediately and the index builds in the background — on a full disk that is 2–3 minutes the first time (the page is usable meanwhile and says the results are partial); later runs warm-start from the cache in seconds.
- **Elevated** (right-click → *Run as administrator*): reads the `C:` `$MFT` and builds a whole-volume index in seconds.
- If `$MFT` is blocked by security software, it degrades to a directory walk of the same volume, explains why, and does not crash.

To index only one folder, use **Settings → Index root → Specific folder** in the page, or `python launch.py C:\Projects` on the command line.

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
# Web UI (no arguments: whole system drive, index builds in the background)
python src/web_ui.py
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
| `--walk <dir>` | Walk the given directory to build the index (default: the whole system drive) |
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

### What gets indexed

**The default scope is the whole system drive** (`%SystemDrive%`, usually `C:\`), in every mode — double-clicking `launch.bat`, `python launch.py`, `python src/web_ui.py`:

| Startup | Index | First run | Later runs |
|---|---|---|---|
| Double-click / no arguments, no admin rights | whole `C:\`, directory walk | page opens in < 1 s, index builds in the background (~2–3 min on a full disk) | warm start from the cache, seconds |
| `launch.bat` → *Run as administrator* | whole `C:\` from `$MFT` | seconds | rebuilt each start (no cache) |
| `--mft D` | whole `D:` (MFT when elevated, walk otherwise) | seconds / background walk | — |
| `--walk <dir>` or Settings → *Specific folder* | that folder | seconds to minutes, in the background | warm start |

Measured on a 1.73 M-entry `C:` (NTFS, SSD+HDD mix): cold walk **132–192 s**, cache **62.8 MB**, warm start **15.4 s**, RSS **+356 MB** during the cold build (down to +63 MB after a warm start).

While the build runs the page stays usable: searching works on what has already been scanned and the result line says `index still building, results may be incomplete`. The status bar shows the progress every second (`Indexing whole drive C: … 512,300 entries (43 s)`). A small line under it shows this process's live disk read/write rate (`Disk I/O: read 45.2 MB/s · write 0.3 MB/s`), refreshed every second — it reads `0.0` when idle and hides itself if the counters are unavailable. Hovering it shows the session totals.

### Choosing the index root from the page

The **Settings** dropdown has two independent groups:

| Group | Meaning |
|---|---|
| **Index root** → *Whole drive* / *Specific folder* | what gets indexed: a whole disk, or one directory (type the path, press Enter or click *Apply*) |
| **Search scope** → *File name only* / *Full path* | where the query is matched: the file name, or the whole path |

Picking *Specific folder* on a whole-drive session narrows the index; picking *Whole drive* again goes back to the whole of the current drive. A bad path is rejected with the reason and **the current index keeps answering** — nothing is swapped.

### Switching volumes (C: → D:)

The **volume selector** next to the search box lists the local fixed disks (`C: (Local Disk)`, `D: (Data)`). Picking one indexes the whole of that volume and swaps it in:

| Situation | Behaviour |
|---|---|
| Elevated, NTFS volume | Rebuilt from `$MFT` in seconds |
| Not elevated | Whole-drive walk, in the background — no administrator rights needed |
| FAT32 / exFAT volume | Walked (there is no `$MFT` to enumerate) |
| The `$MFT` of the target cannot be read | Degrades to walking the **same** volume and says why; it never relabels a different drive |

Only one volume is indexed at a time; the page never merges results from two disks. If you pick a folder scope, the selector shows a `Folder: D:\Projects` entry — picking a drive letter from there goes back to that whole drive.

Everything above is also available over HTTP for scripting:

```bash
curl http://127.0.0.1:8765/api/status                      # scope, building, progress, entries, io
curl "http://127.0.0.1:8765/api/scope?root=D:\Projects"     # POST: index one folder
curl -X POST "http://127.0.0.1:8765/api/switch?volume=D"    # POST: index the whole D:
curl http://127.0.0.1:8765/api/volumes                     # candidates + current volume
```

Switching while a build is running **cancels** the old build (it stops at its next progress tick, writes no cache) and starts the new one; the response carries `cancelled_previous: true`.

### Ports and existing instances

The default port is 8765; if it is taken, the next free port is used. If one of these instances is already running on that port, the browser is pointed at it instead of starting a second one.

## Notes

- **No automatic elevation**: for a whole-volume index, right-click `launch.bat` and choose *Run as administrator*
- When running elevated, UIPI applies: drag and drop from non-elevated windows does not work, and the elevated process cannot see network drives mapped in the normal session
- The fallback walk skips the top-level `AppData` when the root **is** your user directory (about 40% of it is machine cache); a whole-drive or other-folder walk does not skip anything. To search inside AppData, point the tool at it explicitly, e.g. `python src/mft_finder.py --walk %APPDATA%`
- MFT mode rebuilds its index on every start; walk mode persists a cache for fixed local NTFS volumes and warm-starts from it
- Security software may block `FSCTL_ENUM_USN_DATA` (measured with Microsoft PC Manager's filter driver). The tool then falls back to a directory walk and explains why; to use MFT, disable real-time protection temporarily and retry
- In MFT mode, switching to the full-path scope builds and keeps a path resolution table in memory (about 165 MB per million entries). The console reports how many entries were resolved and how long it took; switching back to *file name only* does not release it
- Changing the index root empties the old index the moment the new scope is accepted: a walk cannot answer for a range it has not scanned, and serving the previous drive's paths under a new label would be a lie. MFT builds are synchronous (seconds) so they swap atomically; walk builds fill in progressively and the page marks the results as partial until they finish
- A cancelled build writes nothing to the cache (the exception unwinds before the persist step), so the next start is still a cold build rather than a half-filled cache
- The web UI listens on `127.0.0.1` only and has no authentication, it is for local use
- `cache/` and `logs/` hold runtime artefacts (index cache, crash logs) and can be deleted at any time without affecting functionality

## Files

```text
launch.bat          # double-click entry point (pure ASCII bootstrap layer)
launch.py           # launcher: menu / port probing / spawning children
src/mft_finder.py   # CLI facade (search entry; domain logic lives in mft/)
src/index_cache.py  # cache service facade (build orchestration + SQLite persistence)
src/web_ui.py       # HTTP/window facade (routes, server state, main())
src/native_window.py# pywebview window carrier (optional dependency)
src/mft/            # core library, dependencies point downwards only:
  platform/         #   L0  Windows bindings: $MFT enum, volumes, IO counters
  index/            #   L1  $MFT source (admin) + walk source (fallback)
  cache/            #   L2  SQLite store + warm-start diff
  query.py          #   query parsing + execution
  scope.py          #   index scope model
  page.py           #   default web page resource (inline HTML/JS/CSS)
  iometer.py        #   disk I/O rate meter (sampler injected)
  errors.py         #   cross-layer exception vocabulary (BuildCancelled)
tests/              # one test file per increment (t006 … t019)
```

The three `src/*.py` files are facades: they keep the public names and the
call sites old code and tests rely on, and forward to `mft/`. Nothing under
`mft/` imports a facade — the dependency arrow only points from the delivery
layer down into the library.

## License

[MIT](LICENSE)
