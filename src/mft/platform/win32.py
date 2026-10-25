#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""win32.py — the Windows platform binding layer (ctypes prototypes, constants, structures).

One place for everything that talks to kernel32 directly:
  - volume / $MFT enumeration constants and the USN_RECORD_V2 layout (index.mft_source)
  - drive-type and volume-information prototypes (platform.volumes)
  - this process's cumulative I/O counters (used by the web UI's IoMeter)

Depends on nothing else in the library. Every prototype is declared once here so no
other module has to repeat argtypes/restype (a truncated 64-bit handle was the bug
behind GetProcessIoCounters silently failing — see the comment on that function).
"""

import ctypes
from ctypes import wintypes

GENERIC_READ = 0x80000000
FILE_SHARE_ALL = 0x00000007
OPEN_EXISTING = 3
# FSCTL_ENUM_USN_DATA = CTL_CODE(FILE_DEVICE_FILE_SYSTEM=0x9, 0x2C, METHOD_NEITHER=3, FILE_ANY_ACCESS=0)
#   = (9 << 16) | (0 << 14) | (0x2C << 2) | 3 = 0x000900B3
# 函数号 0x2C 是本控制码的身份标识；换任何别的函数号，内核都会返回
# ERROR_INVALID_FUNCTION(1)（"该文件系统不支持此控制码"），而不是访问权限错误。
# 015 增量修正：此前此处误写 0x00090057（函数号 0x15，非已知 NTFS FSCTL），
# 导致 MFT 模式在任何机器上都失败并被降级逻辑误归因为"安全软件拦截"。
FSCTL_ENUM_USN_DATA = 0x000900B3
ERROR_HANDLE_EOF = 38
INVALID_HANDLE = 0xFFFFFFFFFFFFFFFF
FILE_ATTRIBUTE_DIRECTORY = 0x10

# USN_RECORD_V2 的 FileReferenceNumber / ParentFileReferenceNumber 是 64 位：
# 低 48 位是真正的 MFT FRN，高 16 位是序列号（MFT 记录被复用时递增）。
# 身份语义只看低 48 位，入表前统一屏蔽，否则根目录 FRN=5 永远匹配不上
# （根目录的实际父引用形如 0x0005000000000005）。
FRN_MASK = 0xFFFFFFFFFFFF
# NTFS 根目录的固定 FRN
ROOT_FRN = 5

# USN_RECORD_V2 has a fixed 60-byte header followed by a UTF-16 file name.
USN_RECORD_V2 = "<IHHQQqQIIIIHH"
USN_RECORD_V2_SIZE = 60

DRIVE_FIXED = 3

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
k32.CreateFileW.restype = wintypes.HANDLE
k32.DeviceIoControl.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]
k32.DeviceIoControl.restype = wintypes.BOOL
k32.GetLogicalDrives.restype = wintypes.DWORD
k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
k32.GetDriveTypeW.restype = wintypes.UINT
k32.GetVolumeInformationW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD,
]
k32.GetVolumeInformationW.restype = wintypes.BOOL


class MFT_ENUM_DATA_V0(ctypes.Structure):
    _fields_ = [
        ("StartFileReferenceNumber", ctypes.c_ulonglong),
        ("LowUsn", ctypes.c_longlong),
        ("HighUsn", ctypes.c_longlong),
    ]


class _IO_COUNTERS(ctypes.Structure):
    """IO_COUNTERS (Windows): one process's cumulative I/O, in bytes / op counts."""
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


def process_io_bytes():
    """Cumulative (read_bytes, write_bytes) of *this* process; None when unavailable.

    GetProcessIoCounters on the current pseudo-handle: no privileges, one syscall.
    Read/WriteTransferCount are file/device transfers — for this program that is disk
    traffic ($MFT reads, cache reads/writes). Loopback HTTP answers land in
    OtherTransferCount and are deliberately not shown as disk I/O.
    """
    import sys

    if sys.platform != "win32":
        return None
    try:
        counters = _IO_COUNTERS()
        kernel32 = ctypes.windll.kernel32
        # 64-bit prototypes: windll's default c_int return would truncate the
        # current-process pseudo-handle (-1 as a 64-bit value), and
        # GetProcessIoCounters rejects the truncated handle.
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.GetProcessIoCounters.argtypes = [ctypes.c_void_p,
                                                  ctypes.POINTER(_IO_COUNTERS)]
        kernel32.GetProcessIoCounters.restype = ctypes.c_int
        if not kernel32.GetProcessIoCounters(kernel32.GetCurrentProcess(),
                                             ctypes.byref(counters)):
            return None
        return counters.ReadTransferCount, counters.WriteTransferCount
    except Exception:
        return None
