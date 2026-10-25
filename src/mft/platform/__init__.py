#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""platform — the Windows binding layer of the mft library.

Re-exports the layer's public API. Depends on nothing else in the library:
win32 (ctypes prototypes / constants / process I/O counters) and volumes
(machine and volume facts).
"""

from mft.platform.volumes import (is_admin, list_volumes, system_drive_root,
                                  volume_info)
from mft.platform.win32 import (DRIVE_FIXED, ERROR_HANDLE_EOF, FILE_ATTRIBUTE_DIRECTORY,
                                FILE_SHARE_ALL, FRN_MASK, FSCTL_ENUM_USN_DATA,
                                GENERIC_READ, INVALID_HANDLE, MFT_ENUM_DATA_V0,
                                OPEN_EXISTING, ROOT_FRN, USN_RECORD_V2,
                                USN_RECORD_V2_SIZE, k32, process_io_bytes)
