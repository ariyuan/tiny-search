#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""index — the two index sources of the mft library.

mft_source: whole-volume enumeration via FSCTL_ENUM_USN_DATA ($MFT), needs admin.
walk_source: permission-free recursive scandir, the universal fallback.
Both produce the shapes the cache and query layers consume. Depends on platform
(win32 bindings) and errors (BuildCancelled) only.
"""

from mft.index.mft_source import (MFT_ERROR_HINTS, _mft_error_hint, build_mft_index,
                                 materialize_paths, path_of)
from mft.index.walk_source import (PROGRESS_INTERVAL, build_walk_index, is_junction,
                                   is_user_dir, scan_dir, walk_with_dirs)
