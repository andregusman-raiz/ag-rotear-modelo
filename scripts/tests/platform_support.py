import os
import tempfile
from pathlib import Path


def _probe_symlink(*, target_is_directory: bool) -> bool:
    if not hasattr(os, "symlink"):
        return False
    try:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            link = root / "link"
            if target_is_directory:
                target.mkdir()
            else:
                target.write_bytes(b"probe")
            link.symlink_to(target, target_is_directory=target_is_directory)
            return link.is_symlink()
    except (NotImplementedError, OSError):
        return False


FILE_SYMLINK_AVAILABLE = _probe_symlink(target_is_directory=False)
DIRECTORY_SYMLINK_AVAILABLE = _probe_symlink(target_is_directory=True)
POSIX = os.name == "posix"
WINDOWS = os.name == "nt"
