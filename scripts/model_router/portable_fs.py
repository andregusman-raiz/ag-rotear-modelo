import ctypes
import os
from ctypes import wintypes
from pathlib import Path
from typing import Union


PathLike = Union[str, Path]

MOVEFILE_REPLACE_EXISTING = 0x00000001
MOVEFILE_WRITE_THROUGH = 0x00000008


def _windows_runtime() -> bool:
    return os.name == "nt"


def _load_move_file_ex():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move_file_ex = kernel32.MoveFileExW
    move_file_ex.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move_file_ex.restype = wintypes.BOOL
    return move_file_ex


def replace_file(
    source: PathLike,
    destination: PathLike,
    *,
    replace: bool = True,
) -> None:
    source_path = os.fspath(source)
    destination_path = os.fspath(destination)
    if not _windows_runtime():
        if replace:
            os.replace(source_path, destination_path)
        else:
            os.rename(source_path, destination_path)
        return

    flags = MOVEFILE_WRITE_THROUGH
    if replace:
        flags |= MOVEFILE_REPLACE_EXISTING
    if _load_move_file_ex()(source_path, destination_path, flags):
        return

    error = ctypes.get_last_error()
    raise OSError(error, ctypes.FormatError(error), destination_path)
