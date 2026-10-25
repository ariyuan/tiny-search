#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""query.py — query parsing and execution over either index shape.

Syntax (shared by CLI and web UI): space-separated terms are ANDed, a "!" prefix
excludes, matching is case-insensitive substring. run_search accepts both index
shapes the library produces:

  - dict ({frn: (parent_frn, name, attrs)}): matches record names, or — when a
    {frn: full path} table is supplied and filename_only is False — the full path;
  - list (walk output): matches the basename when filename_only, else the full path.

Depends on: mft.index.mft_source (path_of, for dict hits in filename mode).
"""

import time

from mft.index.mft_source import path_of


def parse_query(q: str):
    must, deny = [], []
    for t in q.split():
        if t.startswith("!") and len(t) > 1:
            deny.append(t[1:].lower())
        else:
            must.append(t.lower())
    return must, deny


def run_search(store, query: str, volume: str, limit: int = 200, filename_only: bool = False,
               paths: dict = None):
    """With filename_only=True a walk index only matches the basename (a dict index only ever
    matches record names anyway).

    Default False: list stores match the full path, dict stores match record names. The
    parameter exists for the "search file names only" option of the web UI.

    paths: the {frn: full path} table for dict stores (the output of materialize_paths).
    When given and filename_only=False, dict mode matches the full path, just like a list
    store; when omitted, behaviour stays "match record names only" and existing callers
    see no change.
    """
    must, deny = parse_query(query)
    if not must:
        # Neither an empty query nor an exclusion-only query (e.g. "!tmp") returns everything:
        # both yield an empty result and are still timed. The check must depend on `must`
        # alone — writing `not must and not deny` would miss the exclusion-only case and
        # wrongly return the whole index.
        return [], 0
    t0 = time.perf_counter()
    hits, results, scanned = [], [], 0
    if isinstance(store, dict):
        match_path = paths is not None and not filename_only
        for frn, (_, name, _) in store.items():
            scanned += 1
            low = (paths[frn] if match_path else name).lower()
            if all(m in low for m in must) and not any(d in low for d in deny):
                hits.append(frn)
                if len(hits) >= limit * 20:
                    break
        # In path mode the resolution table already holds the paths, so no path_of walk needed.
        results = [(paths[f] if match_path else path_of(store, f, volume))
                   for f in hits[:limit]]
    else:
        for p in store:
            scanned += 1
            if filename_only:
                # Take the trailing file name only. Hand-sliced with rfind instead of
                # os.path.basename: ntpath.basename goes through split() internally and
                # measured an order of magnitude slower on million-entry queries.
                i, j = p.rfind("\\"), p.rfind("/")
                low = p[(i if i > j else j) + 1:].lower()
            else:
                low = p.lower()
            if all(m in low for m in must) and not any(d in low for d in deny):
                results.append(p)
                if len(results) >= limit:
                    break
    return results, (time.perf_counter() - t0) * 1000
