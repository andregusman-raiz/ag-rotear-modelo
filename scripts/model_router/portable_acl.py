import ctypes
import os
import stat
from ctypes import wintypes
from pathlib import Path


_PRIVATE_DACL_SDDL = (
    "D:P"
    "(A;OICI;FA;;;OW)"
    "(A;OICI;FA;;;SY)"
    "(A;OICI;FA;;;BA)"
)
_SDDL_REVISION_1 = 1
_SE_FILE_OBJECT = 1
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000


def _windows_runtime() -> bool:
    return os.name == "nt"


def _path_is_link_like(path: Path) -> bool:
    try:
        metadata = os.lstat(str(path))
    except OSError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse_flag
    )


def _load_windows_private_dacl():
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.ULONG),
    )
    convert.restype = wintypes.BOOL

    get_dacl = advapi32.GetSecurityDescriptorDacl
    get_dacl.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    )
    get_dacl.restype = wintypes.BOOL

    security_descriptor = ctypes.c_void_p()
    descriptor_size = wintypes.ULONG()
    if not convert(
        _PRIVATE_DACL_SDDL,
        _SDDL_REVISION_1,
        ctypes.byref(security_descriptor),
        ctypes.byref(descriptor_size),
    ):
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.FormatError(error))
    dacl_present = wintypes.BOOL()
    dacl_defaulted = wintypes.BOOL()
    dacl = ctypes.c_void_p()
    if not get_dacl(
        security_descriptor,
        ctypes.byref(dacl_present),
        ctypes.byref(dacl),
        ctypes.byref(dacl_defaulted),
    ) or not dacl_present.value:
        error = ctypes.get_last_error()
        kernel32.LocalFree(security_descriptor)
        raise OSError(error, ctypes.FormatError(error))
    return advapi32, kernel32, security_descriptor, dacl


def _free_windows_security_descriptor(kernel32, security_descriptor) -> None:
    local_free = kernel32.LocalFree
    local_free.argtypes = (ctypes.c_void_p,)
    local_free.restype = ctypes.c_void_p
    local_free(security_descriptor)


def _set_windows_private_dacl(path: Path) -> None:
    advapi32, kernel32, security_descriptor, dacl = (
        _load_windows_private_dacl()
    )
    try:
        set_security = advapi32.SetNamedSecurityInfoW
        set_security.argtypes = (
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        set_security.restype = wintypes.DWORD
        result = set_security(
            str(path),
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION
            | _PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )
        if result:
            raise OSError(result, ctypes.FormatError(result), str(path))
    finally:
        _free_windows_security_descriptor(kernel32, security_descriptor)


def _set_windows_private_handle(descriptor: int) -> None:
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    final_path = kernel32.GetFinalPathNameByHandleW
    final_path.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    final_path.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = final_path(
        wintypes.HANDLE(msvcrt.get_osfhandle(descriptor)),
        buffer,
        len(buffer),
        0,
    )
    if not length or length >= len(buffer):
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.FormatError(error))
    _set_windows_private_dacl(Path(buffer.value))


def ensure_private_directory(path: Path) -> None:
    directory = Path(path)
    if _path_is_link_like(directory) or not directory.is_dir():
        raise OSError("private directory is invalid")
    if _windows_runtime():
        _set_windows_private_dacl(directory)
    else:
        os.chmod(str(directory), 0o700)


def ensure_private_file(path: Path) -> None:
    target = Path(path)
    if _path_is_link_like(target) or not target.is_file():
        raise OSError("private file is invalid")
    if _windows_runtime():
        _set_windows_private_dacl(target)
    else:
        os.chmod(str(target), 0o600)


def ensure_private_descriptor(descriptor: int, mode: int = 0o600) -> None:
    if _windows_runtime():
        _set_windows_private_handle(descriptor)
    else:
        os.fchmod(descriptor, mode)
