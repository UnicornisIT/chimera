"""Создание каталога с закрытыми правами до записи секретов."""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path


def make_private_directory(path: Path) -> None:
    if sys.platform != "win32":
        path.mkdir(mode=0o700)
        return

    from ctypes import wintypes

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.DWORD),
            ("descriptor", ctypes.c_void_p),
            ("inherit_handle", wintypes.BOOL),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    volume_flags = wintypes.DWORD()
    get_volume = kernel.GetVolumeInformationW
    get_volume.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    if not get_volume(path.anchor, None, 0, None, None, ctypes.byref(volume_flags), None, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    if not volume_flags.value & 0x00000008:  # FS_PERSISTENT_ACLS
        raise OSError("Том не поддерживает постоянные права доступа.")

    descriptor = ctypes.c_void_p()
    convert = security.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    # Защищённая DACL с наследованием для владельца, SYSTEM и администраторов.
    if not convert(
        "D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)", 1, ctypes.byref(descriptor), None
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        create = kernel.CreateDirectoryW
        create.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(SecurityAttributes)]
        if not create(str(path), ctypes.byref(attributes)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        free = kernel.LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(descriptor)
