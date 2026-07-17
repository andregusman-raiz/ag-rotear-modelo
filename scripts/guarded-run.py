#!/usr/bin/env python3
"""Own the private router-input lifecycle and guarantee bounded cleanup."""

import argparse
import errno
import hashlib
import hmac
import json
import os
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Sequence, Set, Tuple

from model_router.portable_acl import ensure_private_directory, ensure_private_file
from model_router.portable_fs import replace_file


PROTOCOL = "ag-model-router-guardian/v1"
READY_PROTOCOL = "ag-model-router-ready/v1"
REQUEST_NAME = "request.json"
TASK_NAME = "task.txt"
READY_NAME = "READY"
READY_STAGING_NAME = ".READY.new"
EXPECTED_INPUTS = frozenset((REQUEST_NAME, TASK_NAME, READY_NAME))
MAX_REQUEST_BYTES = 1024 * 1024
MAX_TASK_BYTES = 1024 * 1024
MAX_READY_BYTES = 512
NONCE_HEX_LENGTH = 64
DEFAULT_PREPARE_TIMEOUT_SECONDS = 60.0
MIN_PREPARE_TIMEOUT_SECONDS = 0.05
MAX_PREPARE_TIMEOUT_SECONDS = 300.0
READY_POLL_SECONDS = 0.02
CHILD_TERMINATE_GRACE_SECONDS = 3.0
CLEANUP_PASSES = 3
CONTROL_FD_ENV = "AG_MODEL_ROUTER_CONTROL_FD"
CONTROL_RECORD_MAX_BYTES = 4096
CONTROL_MAX_GROUPS = 16
CONTROL_POLL_SECONDS = 0.02
PRIVATE_TEMP_ROOT_ENV = "AG_MODEL_ROUTER_PRIVATE_TEMP_ROOT"


def _posix_runtime() -> bool:
    return os.name == "posix"


def _windows_runtime() -> bool:
    return os.name == "nt"


class GuardianError(ValueError):
    pass


class PreparationTimeout(GuardianError):
    pass


class GuardianInterrupted(Exception):
    def __init__(self, signum: int):
        super().__init__("guardian interrupted")
        self.signum = signum


def _timeout(value: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("timeout must be numeric") from None
    if not MIN_PREPARE_TIMEOUT_SECONDS <= parsed <= MAX_PREPARE_TIMEOUT_SECONDS:
        raise argparse.ArgumentTypeError("timeout is outside the safe range")
    return parsed


def _open_directory_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _open_regular_flags() -> int:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    return flags


def _current_uid() -> Optional[int]:
    if hasattr(os, "getuid"):
        return os.getuid()
    return None


def _validate_private_regular_metadata(
    metadata,
    label: str,
    size_limit: int,
) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise GuardianError("%s must be a regular file" % label)
    expected_uid = _current_uid()
    if expected_uid is not None and metadata.st_uid != expected_uid:
        raise GuardianError("%s owner is invalid" % label)
    if _posix_runtime() and stat.S_IMODE(metadata.st_mode) != 0o600:
        raise GuardianError("%s permissions are invalid" % label)
    if metadata.st_size < 0 or metadata.st_size > size_limit:
        raise GuardianError("%s exceeds its size limit" % label)


def _validate_private_directory(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise GuardianError("input directory is invalid")
    expected_uid = _current_uid()
    if expected_uid is not None and metadata.st_uid != expected_uid:
        raise GuardianError("input directory owner is invalid")
    if _posix_runtime() and stat.S_IMODE(metadata.st_mode) != 0o700:
        raise GuardianError("input directory permissions are invalid")


def _is_within(path: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath(
            (os.path.realpath(str(path)), os.path.realpath(str(parent)))
        ) == os.path.realpath(str(parent))
    except ValueError:
        return False


def _validate_launcher(path: Path) -> Path:
    if not path.is_absolute():
        raise GuardianError("launcher must be absolute")
    try:
        metadata = os.stat(str(path), follow_symlinks=False)
    except OSError:
        raise GuardianError("launcher is unavailable") from None
    if not stat.S_ISREG(metadata.st_mode):
        raise GuardianError("launcher is invalid")
    if _posix_runtime() and not os.access(str(path), os.X_OK):
        raise GuardianError("launcher is invalid")
    return path


def _validate_workdir(path: Path) -> Path:
    if not path.is_absolute() or not path.is_dir():
        raise GuardianError("workdir is invalid")
    return path


def _directory_identity(metadata) -> Tuple[int, int]:
    return (metadata.st_dev, metadata.st_ino)


def _metadata_is_reparse_point(metadata) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & reparse_flag)


def _path_is_link_like(path: Path) -> bool:
    try:
        metadata = os.lstat(str(path))
    except OSError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or _metadata_is_reparse_point(metadata)


def _validate_path_components(path: Path) -> None:
    components = []
    current = path
    while current != current.parent:
        components.append(current)
        current = current.parent
    for component in reversed(components):
        if component.exists() and _path_is_link_like(component):
            raise GuardianError("private temp root must not use links")


def _configured_private_temp_root() -> Optional[Path]:
    value = os.environ.get(PRIVATE_TEMP_ROOT_ENV)
    if value is None or not value.strip():
        return None
    return Path(value)


def _default_private_temp_root() -> Path:
    user_id = _current_uid()
    if user_id is None:
        identity = (
            os.environ.get("USERNAME")
            or os.environ.get("USERPROFILE")
            or "windows-user"
        )
        suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    else:
        suffix = str(user_id)
    return Path(tempfile.gettempdir()) / (
        "ag-model-router-private-" + suffix
    )


def _resolve_private_temp_root(
    workdir: Path,
    configured: Optional[Path],
) -> Path:
    candidate = (
        configured
        if configured is not None
        else _default_private_temp_root()
    )
    if not candidate.is_absolute():
        raise GuardianError("private temp root must be absolute")
    candidate = Path(os.path.abspath(str(candidate)))
    try:
        candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
        if _windows_runtime():
            _validate_path_components(candidate)
        elif _path_is_link_like(candidate):
            raise GuardianError("private temp root must not use links")
        metadata = os.lstat(str(candidate))
    except GuardianError:
        raise
    except OSError:
        raise GuardianError("private temp root is unavailable") from None
    if not stat.S_ISDIR(metadata.st_mode) or _metadata_is_reparse_point(metadata):
        raise GuardianError("private temp root is invalid")
    if _is_within(candidate, workdir):
        raise GuardianError("private temp root must be outside workdir")
    try:
        ensure_private_directory(candidate)
    except OSError:
        raise GuardianError("private temp root could not be secured") from None
    return candidate


def _create_input_directory(
    workdir: Path,
    private_temp_root: Optional[Path] = None,
) -> Tuple[Path, int, int, str, Tuple[int, int]]:
    if private_temp_root is None:
        private_temp_root = _resolve_private_temp_root(workdir, None)
    input_dir = Path(
        tempfile.mkdtemp(
            prefix="ag-model-router.",
            dir=str(private_temp_root),
        )
    )
    descriptor = -1
    parent_descriptor = -1
    try:
        if _is_within(input_dir, workdir):
            os.rmdir(str(input_dir))
            raise GuardianError("input directory must be outside workdir")
        ensure_private_directory(input_dir)
        parent_descriptor = os.open(
            str(input_dir.parent),
            _open_directory_flags(),
        )
        descriptor = os.open(str(input_dir), _open_directory_flags())
        _validate_private_directory(descriptor)
        identity = _directory_identity(os.fstat(descriptor))
        linked = os.stat(
            input_dir.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if _directory_identity(linked) != identity:
            raise GuardianError("input directory identity changed")
        return (
            input_dir,
            descriptor,
            parent_descriptor,
            input_dir.name,
            identity,
        )
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            try:
                os.rmdir(input_dir.name, dir_fd=parent_descriptor)
            except OSError:
                pass
            os.close(parent_descriptor)
        else:
            try:
                os.rmdir(str(input_dir))
            except OSError:
                pass
        raise


def _create_input_directory_path(
    workdir: Path,
    private_temp_root: Path,
) -> Tuple[Path, Tuple[int, int]]:
    input_dir = Path(
        tempfile.mkdtemp(
            prefix="ag-model-router.",
            dir=str(private_temp_root),
        )
    )
    try:
        if _is_within(input_dir, workdir):
            os.rmdir(str(input_dir))
            raise GuardianError("input directory must be outside workdir")
        ensure_private_directory(input_dir)
        metadata = os.stat(str(input_dir), follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode):
            raise GuardianError("input directory is invalid")
        return input_dir, _directory_identity(metadata)
    except Exception:
        try:
            os.rmdir(str(input_dir))
        except OSError:
            pass
        raise


def _open_input(descriptor: int, name: str, limit: int) -> int:
    opened = -1
    try:
        opened = os.open(
            name,
            _open_regular_flags(),
            dir_fd=descriptor,
        )
        _validate_private_regular_metadata(os.fstat(opened), name, limit)
        return opened
    except GuardianError:
        if opened >= 0:
            os.close(opened)
        raise
    except OSError:
        if opened >= 0:
            os.close(opened)
        raise GuardianError("router input is invalid") from None


def _read_bounded(descriptor: int, limit: int) -> bytes:
    chunks: List[bytes] = []
    remaining = limit + 1
    while remaining > 0:
        chunk = os.read(descriptor, min(65536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) > limit:
        raise GuardianError("router input exceeds its size limit")
    return payload


class _DuplicateManifestKey(ValueError):
    pass


def _strict_manifest_object(pairs):
    payload = {}
    for key, value in pairs:
        if key in payload:
            raise _DuplicateManifestKey()
        payload[key] = value
    return payload


def _reject_manifest_constant(_value):
    raise ValueError("non-finite manifest number")


def _canonical_manifest_bytes(payload) -> bytes:
    return (
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        + b"\n"
    )


def _is_lower_hex(value: object, length: int) -> bool:
    if not isinstance(value, str) or len(value) != length:
        return False
    return all(character in "0123456789abcdef" for character in value)


def _manifest_entry(payload, label: str, limit: int):
    if not isinstance(payload, dict) or frozenset(payload) != frozenset(
        ("sha256", "size")
    ):
        raise GuardianError("READY manifest is invalid")
    size = payload.get("size")
    digest = payload.get("sha256")
    if type(size) is not int or not 0 < size <= limit:
        raise GuardianError("READY manifest is invalid")
    if not _is_lower_hex(digest, 64):
        raise GuardianError("READY manifest is invalid")
    return {"sha256": digest, "size": size}


def _parse_ready_manifest(document: bytes, expected_nonce: str):
    if not _is_lower_hex(expected_nonce, NONCE_HEX_LENGTH):
        raise GuardianError("READY manifest is invalid")
    try:
        payload = json.loads(
            document.decode("ascii"),
            object_pairs_hook=_strict_manifest_object,
            parse_constant=_reject_manifest_constant,
        )
    except (TypeError, ValueError, UnicodeError):
        raise GuardianError("READY manifest is invalid") from None
    if not isinstance(payload, dict) or frozenset(payload) != frozenset(
        ("nonce", "protocol", "request", "task")
    ):
        raise GuardianError("READY manifest is invalid")
    if payload.get("protocol") != READY_PROTOCOL:
        raise GuardianError("READY manifest is invalid")
    nonce = payload.get("nonce")
    if not _is_lower_hex(nonce, NONCE_HEX_LENGTH) or not hmac.compare_digest(
        nonce,
        expected_nonce,
    ):
        raise GuardianError("READY manifest is invalid")
    normalized = {
        "nonce": nonce,
        "protocol": READY_PROTOCOL,
        "request": _manifest_entry(
            payload.get("request"),
            REQUEST_NAME,
            MAX_REQUEST_BYTES,
        ),
        "task": _manifest_entry(
            payload.get("task"),
            TASK_NAME,
            MAX_TASK_BYTES,
        ),
    }
    if _canonical_manifest_bytes(normalized) != document:
        raise GuardianError("READY manifest is invalid")
    return normalized


def _build_ready_manifest(
    request_snapshot: bytes,
    task_snapshot: bytes,
    nonce: str,
) -> bytes:
    if not _is_lower_hex(nonce, NONCE_HEX_LENGTH):
        raise GuardianError("READY nonce is invalid")
    payload = {
        "nonce": nonce,
        "protocol": READY_PROTOCOL,
        "request": {
            "sha256": hashlib.sha256(request_snapshot).hexdigest(),
            "size": len(request_snapshot),
        },
        "task": {
            "sha256": hashlib.sha256(task_snapshot).hexdigest(),
            "size": len(task_snapshot),
        },
    }
    return _canonical_manifest_bytes(payload)


def _task_metadata_fingerprint(metadata) -> Tuple[object, ...]:
    field_groups = (
        ("st_dev",),
        ("st_ino",),
        ("st_size",),
        ("st_mtime_ns", "st_mtime"),
        ("st_ctime_ns", "st_ctime"),
    )
    values = []
    for names in field_groups:
        for name in names:
            if hasattr(metadata, name):
                values.append((name, getattr(metadata, name)))
                break
    return tuple(values)


def _read_snapshot_exact(descriptor: int, expected_size: int) -> bytes:
    chunks: List[bytes] = []
    remaining = expected_size
    while remaining > 0:
        chunk = os.read(descriptor, min(65536, remaining))
        if not chunk:
            raise GuardianError("task changed during snapshot")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _snapshot_input_descriptor(
    descriptor: int,
    label: str,
    limit: int,
) -> bytes:
    try:
        before = os.fstat(descriptor)
        _validate_private_regular_metadata(before, label, limit)
        if before.st_size == 0:
            raise GuardianError("router input must not be empty")
        expected = _task_metadata_fingerprint(before)
        snapshots = []
        for _attempt in range(2):
            os.lseek(descriptor, 0, os.SEEK_SET)
            snapshot = _read_snapshot_exact(descriptor, before.st_size)
            after = os.fstat(descriptor)
            _validate_private_regular_metadata(after, label, limit)
            if _task_metadata_fingerprint(after) != expected:
                raise GuardianError("task changed during snapshot")
            snapshots.append(snapshot)
        if snapshots[0] != snapshots[1]:
            raise GuardianError("task changed during snapshot")
        return snapshots[1]
    except GuardianError:
        raise
    except OSError:
        raise GuardianError("task snapshot failed") from None


def _snapshot_task_descriptor(descriptor: int) -> bytes:
    return _snapshot_input_descriptor(descriptor, TASK_NAME, MAX_TASK_BYTES)


def _ready_exists(descriptor: int) -> bool:
    ready = -1
    try:
        ready = os.open(
            READY_NAME,
            _open_regular_flags(),
            dir_fd=descriptor,
        )
        return READY_STAGING_NAME not in os.listdir(descriptor)
    except FileNotFoundError:
        return False
    except OSError as error:
        if error.errno == errno.ENOENT:
            return False
        raise GuardianError("READY marker is invalid") from None
    finally:
        if ready >= 0:
            os.close(ready)


def _wait_until_ready(descriptor: int, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not _ready_exists(descriptor):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PreparationTimeout("preparation timeout")
        time.sleep(min(READY_POLL_SECONDS, remaining))


def _validate_inputs(
    descriptor: int,
    expected_nonce: str,
) -> Tuple[bytes, bytes]:
    try:
        entries = frozenset(os.listdir(descriptor))
    except OSError:
        raise GuardianError("router inputs could not be listed") from None
    if entries != EXPECTED_INPUTS:
        raise GuardianError("router input set is invalid")
    request = -1
    task = -1
    ready = -1
    try:
        request = _open_input(descriptor, REQUEST_NAME, MAX_REQUEST_BYTES)
        task = _open_input(descriptor, TASK_NAME, MAX_TASK_BYTES)
        ready = _open_input(descriptor, READY_NAME, MAX_READY_BYTES)
        manifest = _parse_ready_manifest(
            _read_bounded(ready, MAX_READY_BYTES),
            expected_nonce,
        )
        request_snapshot = _snapshot_input_descriptor(
            request,
            REQUEST_NAME,
            MAX_REQUEST_BYTES,
        )
        task_snapshot = _snapshot_task_descriptor(task)
        for label, snapshot in (
            ("request", request_snapshot),
            ("task", task_snapshot),
        ):
            committed = manifest[label]
            if len(snapshot) != committed["size"] or not hmac.compare_digest(
                hashlib.sha256(snapshot).hexdigest(),
                committed["sha256"],
            ):
                raise GuardianError("router input changed after READY")
        return request_snapshot, task_snapshot
    finally:
        if request >= 0:
            os.close(request)
        if task >= 0:
            os.close(task)
        if ready >= 0:
            os.close(ready)


def _open_input_path(input_dir: Path, name: str, limit: int) -> int:
    path = input_dir / name
    opened = -1
    try:
        if _path_is_link_like(path):
            raise GuardianError("router input is invalid")
        opened = os.open(str(path), _open_regular_flags())
        _validate_private_regular_metadata(os.fstat(opened), name, limit)
        return opened
    except GuardianError:
        if opened >= 0:
            os.close(opened)
        raise
    except OSError:
        if opened >= 0:
            os.close(opened)
        raise GuardianError("router input is invalid") from None


def _ready_exists_path(input_dir: Path) -> bool:
    if (input_dir / READY_STAGING_NAME).exists():
        return False
    ready_path = input_dir / READY_NAME
    if not ready_path.exists():
        return False
    if _path_is_link_like(ready_path):
        raise GuardianError("READY marker is invalid")
    return True


def _wait_until_ready_path(input_dir: Path, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not _ready_exists_path(input_dir):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PreparationTimeout("preparation timeout")
        time.sleep(min(READY_POLL_SECONDS, remaining))


def _validate_inputs_path(
    input_dir: Path,
    expected_nonce: str,
) -> Tuple[bytes, bytes]:
    try:
        entries = frozenset(entry.name for entry in input_dir.iterdir())
    except OSError:
        raise GuardianError("router inputs could not be listed") from None
    if entries != EXPECTED_INPUTS:
        raise GuardianError("router input set is invalid")
    request = -1
    task = -1
    ready = -1
    try:
        request = _open_input_path(input_dir, REQUEST_NAME, MAX_REQUEST_BYTES)
        task = _open_input_path(input_dir, TASK_NAME, MAX_TASK_BYTES)
        ready = _open_input_path(input_dir, READY_NAME, MAX_READY_BYTES)
        manifest = _parse_ready_manifest(
            _read_bounded(ready, MAX_READY_BYTES),
            expected_nonce,
        )
        request_snapshot = _snapshot_input_descriptor(
            request,
            REQUEST_NAME,
            MAX_REQUEST_BYTES,
        )
        task_snapshot = _snapshot_task_descriptor(task)
        for label, snapshot in (
            ("request", request_snapshot),
            ("task", task_snapshot),
        ):
            committed = manifest[label]
            if len(snapshot) != committed["size"] or not hmac.compare_digest(
                hashlib.sha256(snapshot).hexdigest(),
                committed["sha256"],
            ):
                raise GuardianError("router input changed after READY")
        return request_snapshot, task_snapshot
    finally:
        for descriptor in (request, task, ready):
            if descriptor >= 0:
                os.close(descriptor)


def _scrub_directory(descriptor: int) -> bool:
    clean = True
    for _attempt in range(CLEANUP_PASSES):
        try:
            entries = tuple(os.listdir(descriptor))
        except OSError:
            return False
        if not entries:
            return clean
        removed_any = False
        for name in entries:
            try:
                metadata = os.stat(
                    name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                if stat.S_ISDIR(metadata.st_mode):
                    clean = False
                    continue
                os.unlink(name, dir_fd=descriptor)
                removed_any = True
            except FileNotFoundError:
                removed_any = True
            except OSError:
                clean = False
        if not removed_any:
            break
    try:
        return clean and not os.listdir(descriptor)
    except OSError:
        return False


def _directory_path_matches(
    input_dir: Path,
    identity: Tuple[int, int],
) -> bool:
    try:
        metadata = os.stat(str(input_dir), follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and not _metadata_is_reparse_point(metadata)
        and not stat.S_ISLNK(metadata.st_mode)
        and _directory_identity(metadata) == identity
    )


def _scrub_directory_path(
    input_dir: Path,
    expected_identity: Optional[Tuple[int, int]] = None,
) -> bool:
    clean = True
    for _attempt in range(CLEANUP_PASSES):
        if expected_identity is not None and not _directory_path_matches(
            input_dir,
            expected_identity,
        ):
            return False
        try:
            entries = tuple(input_dir.iterdir())
        except OSError:
            return False
        if not entries:
            return clean
        removed_any = False
        for entry in entries:
            if expected_identity is not None and not _directory_path_matches(
                input_dir,
                expected_identity,
            ):
                return False
            try:
                metadata = os.stat(str(entry), follow_symlinks=False)
                if stat.S_ISDIR(metadata.st_mode):
                    clean = False
                    continue
                entry.unlink()
                removed_any = True
            except FileNotFoundError:
                removed_any = True
            except OSError:
                clean = False
        if not removed_any:
            break
    try:
        return clean and not tuple(input_dir.iterdir())
    except OSError:
        return False


def _scrub_bound_directory_path(
    input_dir: Path,
    identity: Tuple[int, int],
) -> bool:
    return _scrub_directory_path(input_dir, expected_identity=identity)


def _remove_bound_directory(
    parent_descriptor: int,
    basename: str,
    identity: Tuple[int, int],
) -> bool:
    try:
        linked = os.stat(
            basename,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError:
        return False
    if not stat.S_ISDIR(linked.st_mode) or _directory_identity(linked) != identity:
        return False
    try:
        os.rmdir(basename, dir_fd=parent_descriptor)
        return True
    except OSError:
        return False


def _remove_path_directory(input_dir: Path, identity: Tuple[int, int]) -> bool:
    try:
        linked = os.stat(str(input_dir), follow_symlinks=False)
    except OSError:
        return False
    if (
        not stat.S_ISDIR(linked.st_mode)
        or _metadata_is_reparse_point(linked)
        or _directory_identity(linked) != identity
    ):
        return False
    try:
        input_dir.rmdir()
        return True
    except OSError:
        return False


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise GuardianError("READY manifest write failed")
        offset += written


def publish_ready(input_dir: Path, nonce: str) -> None:
    if not input_dir.is_absolute() or not _is_lower_hex(nonce, NONCE_HEX_LENGTH):
        raise GuardianError("READY publication input is invalid")
    if not _posix_runtime():
        return _publish_ready_path(input_dir, nonce)
    descriptor = -1
    staged = -1
    request = -1
    task = -1
    published = False
    try:
        descriptor = os.open(str(input_dir), _open_directory_flags())
        _validate_private_directory(descriptor)
        if frozenset(os.listdir(descriptor)) != frozenset((REQUEST_NAME, TASK_NAME)):
            raise GuardianError("READY publication input set is invalid")
        request = _open_input(descriptor, REQUEST_NAME, MAX_REQUEST_BYTES)
        try:
            task = _open_input(descriptor, TASK_NAME, MAX_TASK_BYTES)
            request_snapshot = _snapshot_input_descriptor(
                request,
                REQUEST_NAME,
                MAX_REQUEST_BYTES,
            )
            task_snapshot = _snapshot_task_descriptor(task)
        finally:
            if request >= 0:
                os.close(request)
                request = -1
            if task >= 0:
                os.close(task)
                task = -1
        manifest = _build_ready_manifest(request_snapshot, task_snapshot, nonce)
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
        )
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        staged = os.open(
            READY_STAGING_NAME,
            flags,
            0o600,
            dir_fd=descriptor,
        )
        _write_all(staged, manifest)
        os.fsync(staged)
        os.close(staged)
        staged = -1
        os.link(
            READY_STAGING_NAME,
            READY_NAME,
            src_dir_fd=descriptor,
            dst_dir_fd=descriptor,
            follow_symlinks=False,
        )
        os.fsync(descriptor)
        os.unlink(READY_STAGING_NAME, dir_fd=descriptor)
        published = True
    except GuardianError:
        raise
    except OSError:
        raise GuardianError("READY publication failed") from None
    finally:
        if staged >= 0:
            try:
                os.close(staged)
            except OSError:
                pass
        if request >= 0:
            try:
                os.close(request)
            except OSError:
                pass
        if task >= 0:
            try:
                os.close(task)
            except OSError:
                pass
        if descriptor >= 0:
            ready_absent = published
            if not published:
                try:
                    os.unlink(READY_NAME, dir_fd=descriptor)
                    ready_absent = True
                except FileNotFoundError:
                    ready_absent = True
                except OSError:
                    ready_absent = False
            if ready_absent:
                try:
                    os.unlink(READY_STAGING_NAME, dir_fd=descriptor)
                except OSError:
                    pass
            try:
                os.close(descriptor)
            except OSError:
                pass


def _publish_ready_path(input_dir: Path, nonce: str) -> None:
    if _path_is_link_like(input_dir) or not input_dir.is_dir():
        raise GuardianError("READY publication input is invalid")
    staged_path = input_dir / READY_STAGING_NAME
    ready_path = input_dir / READY_NAME
    published = False
    request = -1
    task = -1
    staged = -1
    try:
        if frozenset(entry.name for entry in input_dir.iterdir()) != frozenset(
            (REQUEST_NAME, TASK_NAME)
        ):
            raise GuardianError("READY publication input set is invalid")
        request = _open_input_path(input_dir, REQUEST_NAME, MAX_REQUEST_BYTES)
        task = _open_input_path(input_dir, TASK_NAME, MAX_TASK_BYTES)
        request_snapshot = _snapshot_input_descriptor(
            request,
            REQUEST_NAME,
            MAX_REQUEST_BYTES,
        )
        task_snapshot = _snapshot_task_descriptor(task)
        manifest = _build_ready_manifest(request_snapshot, task_snapshot, nonce)
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
        )
        staged = os.open(str(staged_path), flags, 0o600)
        _write_all(staged, manifest)
        os.fsync(staged)
        os.close(staged)
        staged = -1
        ensure_private_file(staged_path)
        replace_file(staged_path, ready_path)
        published = True
    except GuardianError:
        raise
    except OSError:
        raise GuardianError("READY publication failed") from None
    finally:
        for descriptor in (request, task, staged):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        if not published:
            for path in (ready_path, staged_path):
                try:
                    path.unlink()
                except OSError:
                    pass


def _signal_handler(signum, _frame) -> None:
    raise GuardianInterrupted(signum)


def _handled_signals() -> Tuple[int, ...]:
    names = ("SIGTERM", "SIGINT", "SIGHUP")
    return tuple(getattr(signal, name) for name in names if hasattr(signal, name))


def _install_signal_handlers(signals: Sequence[int]) -> None:
    for handled in signals:
        signal.signal(handled, _signal_handler)


def _ignore_signals(signals: Sequence[int]) -> None:
    for handled in signals:
        signal.signal(handled, signal.SIG_IGN)


def _open_control_pipe() -> Tuple[int, int]:
    if not _posix_runtime():
        return (-1, -1)
    read_descriptor, write_descriptor = os.pipe()
    try:
        os.set_blocking(read_descriptor, False)
        return read_descriptor, write_descriptor
    except Exception:
        os.close(read_descriptor)
        os.close(write_descriptor)
        raise


def _close_descriptor(descriptor: int) -> None:
    if descriptor >= 0:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _parse_control_line(line: bytes) -> Optional[Tuple[str, int]]:
    if len(line) < 2:
        return None
    marker = chr(line[0])
    if marker not in ("+", "-"):
        return None
    raw_pid = line[1:]
    if not raw_pid or not raw_pid.isdigit():
        return None
    try:
        pid = int(raw_pid)
    except ValueError:
        return None
    if pid <= 1 or pid == os.getpid():
        return None
    return (marker, pid)


def _drain_control_pipe(
    descriptor: int,
    active_groups: Set[int],
    pending: bytearray,
) -> None:
    if descriptor < 0:
        return
    while True:
        try:
            chunk = os.read(descriptor, CONTROL_RECORD_MAX_BYTES)
        except BlockingIOError:
            break
        except OSError:
            break
        if not chunk:
            break
        if len(pending) + len(chunk) > CONTROL_RECORD_MAX_BYTES:
            pending.clear()
            chunk = chunk[-CONTROL_RECORD_MAX_BYTES:]
        pending.extend(chunk)
        while True:
            try:
                newline = pending.index(ord("\n"))
            except ValueError:
                break
            line = bytes(pending[:newline])
            del pending[: newline + 1]
            parsed = _parse_control_line(line)
            if parsed is None:
                continue
            marker, pid = parsed
            if marker == "+":
                if len(active_groups) < CONTROL_MAX_GROUPS:
                    active_groups.add(pid)
            else:
                active_groups.discard(pid)


def _signal_registered_groups(active_groups: Set[int], sent_signal: int) -> None:
    for pgid in tuple(active_groups):
        try:
            os.killpg(pgid, sent_signal)
        except ProcessLookupError:
            active_groups.discard(pgid)
        except OSError:
            pass


def _signal_child_group(process: subprocess.Popen, sent_signal: int) -> None:
    if _windows_runtime():
        force = sent_signal == signal.SIGKILL
        command = ["taskkill", "/PID", str(process.pid), "/T"]
        if force:
            command.append("/F")
        try:
            subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                shell=False,
            )
            return
        except OSError:
            pass
    try:
        if _posix_runtime() and hasattr(os, "killpg"):
            os.killpg(process.pid, sent_signal)
        else:
            if sent_signal == signal.SIGKILL:
                process.kill()
            else:
                process.terminate()
    except ProcessLookupError:
        return


def _terminate_child(process: subprocess.Popen, control_read_descriptor: int = -1) -> None:
    active_groups: Set[int] = set()
    pending = bytearray()
    _drain_control_pipe(control_read_descriptor, active_groups, pending)
    if process.poll() is not None and not active_groups:
        return
    _signal_child_group(process, signal.SIGTERM)
    _signal_registered_groups(active_groups, signal.SIGTERM)
    deadline = time.monotonic() + CHILD_TERMINATE_GRACE_SECONDS
    while time.monotonic() < deadline:
        _drain_control_pipe(control_read_descriptor, active_groups, pending)
        _signal_registered_groups(active_groups, signal.SIGTERM)
        if process.poll() is not None and not active_groups:
            return
        time.sleep(CONTROL_POLL_SECONDS)
    _drain_control_pipe(control_read_descriptor, active_groups, pending)
    _signal_child_group(process, signal.SIGKILL)
    _signal_registered_groups(active_groups, signal.SIGKILL)
    try:
        process.wait(timeout=CHILD_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    _drain_control_pipe(control_read_descriptor, active_groups, pending)
    _signal_registered_groups(active_groups, signal.SIGKILL)


def _child_environment(control_write_descriptor: int = -1) -> dict:
    environment = os.environ.copy()
    environment.pop("TASK_TEXT", None)
    environment.pop("TASK_REQUEST_JSON", None)
    if control_write_descriptor >= 0:
        environment[CONTROL_FD_ENV] = str(control_write_descriptor)
    else:
        environment.pop(CONTROL_FD_ENV, None)
    return environment


def _anonymous_request_file(snapshot: bytes, private_temp_root: Path):
    if not _posix_runtime():
        raise GuardianError("anonymous request transport requires POSIX")
    handle = tempfile.TemporaryFile(
        mode="w+b",
        dir=str(private_temp_root),
    )
    try:
        handle.write(snapshot)
        handle.flush()
        os.fsync(handle.fileno())
        handle.seek(0)
        metadata = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size != len(snapshot)
            or metadata.st_nlink != 0
        ):
            raise GuardianError("anonymous request transport is invalid")
        return handle
    except Exception:
        handle.close()
        raise


class _RequestTransport:
    def __init__(
        self,
        argv: Sequence[str],
        pass_fds: Sequence[int] = (),
        handle=None,
        path: Optional[Path] = None,
    ):
        self.argv = list(argv)
        self.pass_fds = tuple(pass_fds)
        self.handle = handle
        self.path = path

    def close(self) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None
        if self.path is not None:
            try:
                self.path.unlink()
            except OSError:
                pass
            self.path = None


def _request_transport(
    snapshot: bytes,
    private_temp_root: Path,
) -> _RequestTransport:
    if _posix_runtime():
        handle = _anonymous_request_file(snapshot, private_temp_root)
        descriptor = handle.fileno()
        return _RequestTransport(
            ["--request-fd", str(descriptor)],
            pass_fds=(descriptor,),
            handle=handle,
        )
    descriptor = -1
    path = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="ag-model-router-request-",
            suffix=".json",
            dir=str(private_temp_root),
        )
        path = Path(temporary_name)
        _write_all(descriptor, snapshot)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        ensure_private_file(path)
        return _RequestTransport(["--request", str(path)], path=path)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass
        raise


def _launcher_argv(launcher: Path) -> List[str]:
    if launcher.suffix.lower() == ".py":
        return [sys.executable, str(launcher)]
    return [str(launcher)]


def _default_launcher() -> Path:
    name = "run-route.py" if _windows_runtime() else "run-route.sh"
    return Path(__file__).with_name(name)


def _child_argv(args, request_argv: Sequence[str]) -> List[str]:
    return [
        *_launcher_argv(args.launcher),
        "run",
        *request_argv,
        "--workdir",
        str(args.workdir),
        "--sandbox",
        args.sandbox,
        "--approval-policy",
        args.approval_policy,
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="guarded-run.py")
    parser.add_argument(
        "--launcher",
        type=Path,
        default=_default_launcher(),
    )
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument(
        "--private-temp-root",
        type=Path,
        default=_configured_private_temp_root(),
    )
    parser.add_argument(
        "--sandbox",
        required=True,
        choices=("read-only", "workspace-write", "danger-full-access"),
    )
    parser.add_argument(
        "--approval-policy",
        required=True,
        choices=("untrusted", "on-request", "never"),
    )
    parser.add_argument(
        "--prepare-timeout",
        type=_timeout,
        default=DEFAULT_PREPARE_TIMEOUT_SECONDS,
    )
    return parser


def _spawn_child(
    args,
    request_transport: _RequestTransport,
    control_write_descriptor: int,
):
    pass_descriptors = list(request_transport.pass_fds)
    if control_write_descriptor >= 0:
        pass_descriptors.append(control_write_descriptor)
    process_options = {
        "cwd": str(args.workdir),
        "env": _child_environment(control_write_descriptor),
        "stdin": subprocess.PIPE,
        "shell": False,
        "close_fds": True,
        "start_new_session": _posix_runtime(),
    }
    if pass_descriptors:
        process_options["pass_fds"] = tuple(pass_descriptors)
    if _windows_runtime() and hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(
        _child_argv(args, request_transport.argv),
        **process_options,
    )


def _run_path_lifecycle(args, handled_signals: Sequence[int]) -> int:
    input_dir: Optional[Path] = None
    directory_identity: Optional[Tuple[int, int]] = None
    directory_removed = False
    request_transport = None
    child: Optional[subprocess.Popen] = None
    exit_code = 125
    try:
        args.launcher = _validate_launcher(args.launcher)
        args.workdir = _validate_workdir(args.workdir)
        args.private_temp_root = _resolve_private_temp_root(
            args.workdir,
            args.private_temp_root,
        )
        input_dir, directory_identity = _create_input_directory_path(
            args.workdir,
            args.private_temp_root,
        )
        ready_nonce = secrets.token_hex(NONCE_HEX_LENGTH // 2)
        event = {
            "event": "input-ready",
            "guardian_pid": os.getpid(),
            "protocol": PROTOCOL,
            "ready_nonce": ready_nonce,
            "input_dir": str(input_dir),
            "request_path": str(input_dir / REQUEST_NAME),
            "task_path": str(input_dir / TASK_NAME),
            "ready_path": str(input_dir / READY_NAME),
            "transport": "path",
        }
        print(json.dumps(event, sort_keys=True, separators=(",", ":")), flush=True)
        _wait_until_ready_path(input_dir, args.prepare_timeout)
        request_snapshot, task_snapshot = _validate_inputs_path(
            input_dir,
            ready_nonce,
        )
        if not _scrub_bound_directory_path(input_dir, directory_identity):
            raise GuardianError("input directory cleanup failed")
        directory_removed = _remove_path_directory(input_dir, directory_identity)
        if not directory_removed:
            raise GuardianError("input directory identity changed")
        request_transport = _request_transport(
            request_snapshot,
            args.private_temp_root,
        )
        deferred_signals: List[int] = []
        spawn_error: Optional[BaseException] = None

        def defer_signal(signum, _frame) -> None:
            deferred_signals.append(signum)

        for handled in handled_signals:
            signal.signal(handled, defer_signal)
        try:
            child = _spawn_child(args, request_transport, -1)
        except BaseException as error:
            spawn_error = error
        finally:
            _install_signal_handlers(handled_signals)
        if deferred_signals:
            raise GuardianInterrupted(deferred_signals[0])
        if spawn_error is not None:
            raise spawn_error
        child.communicate(input=task_snapshot)
        child_status = child.returncode
        if child_status is None:
            child_status = child.wait()
        exit_code = (
            128 + abs(child_status)
            if child_status < 0
            else child_status
        )
    except GuardianInterrupted as interrupted:
        exit_code = 128 + interrupted.signum
    except PreparationTimeout:
        print("guardian error: preparation timeout", file=sys.stderr)
        exit_code = 124
    except GuardianError:
        print("guardian error: invalid input", file=sys.stderr)
        exit_code = 2
    except Exception:
        print("guardian error: operation failed", file=sys.stderr)
        exit_code = 125
    finally:
        _ignore_signals(handled_signals)
        if child is not None:
            _terminate_child(child)
        if request_transport is not None:
            request_transport.close()
        if input_dir is not None and not directory_removed:
            if (
                directory_identity is not None
                and _scrub_bound_directory_path(input_dir, directory_identity)
            ):
                directory_removed = _remove_path_directory(
                    input_dir,
                    directory_identity,
                )
            if not directory_removed:
                print("guardian error: cleanup binding failed", file=sys.stderr)
                exit_code = 125
    return exit_code


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    handled_signals = _handled_signals()
    _install_signal_handlers(handled_signals)
    if not _posix_runtime():
        return _run_path_lifecycle(args, handled_signals)
    input_dir: Optional[Path] = None
    directory_descriptor = -1
    parent_descriptor = -1
    directory_basename = ""
    directory_identity: Optional[Tuple[int, int]] = None
    directory_removed = False
    cleanup_binding_failed = False
    request_snapshot: Optional[bytes] = None
    task_snapshot: Optional[bytes] = None
    request_transport = None
    control_read_descriptor = -1
    control_write_descriptor = -1
    child: Optional[subprocess.Popen] = None
    exit_code = 125
    try:
        args.launcher = _validate_launcher(args.launcher)
        args.workdir = _validate_workdir(args.workdir)
        args.private_temp_root = _resolve_private_temp_root(
            args.workdir,
            args.private_temp_root,
        )
        (
            input_dir,
            directory_descriptor,
            parent_descriptor,
            directory_basename,
            directory_identity,
        ) = _create_input_directory(args.workdir, args.private_temp_root)
        ready_nonce = secrets.token_hex(NONCE_HEX_LENGTH // 2)
        event = {
            "event": "input-ready",
            "guardian_pid": os.getpid(),
            "protocol": PROTOCOL,
            "ready_nonce": ready_nonce,
            "input_dir": str(input_dir),
            "request_path": str(input_dir / REQUEST_NAME),
            "task_path": str(input_dir / TASK_NAME),
            "ready_path": str(input_dir / READY_NAME),
        }
        print(json.dumps(event, sort_keys=True, separators=(",", ":")), flush=True)
        _wait_until_ready(directory_descriptor, args.prepare_timeout)
        request_snapshot, task_snapshot = _validate_inputs(
            directory_descriptor,
            ready_nonce,
        )
        if not _scrub_directory(directory_descriptor):
            raise GuardianError("input directory cleanup failed")
        directory_removed = _remove_bound_directory(
            parent_descriptor,
            directory_basename,
            directory_identity,
        )
        cleanup_binding_failed = not directory_removed
        if cleanup_binding_failed:
            raise GuardianError("input directory identity changed")
        request_transport = _request_transport(
            request_snapshot,
            args.private_temp_root,
        )
        control_read_descriptor, control_write_descriptor = _open_control_pipe()
        deferred_signals: List[int] = []
        spawn_error: Optional[BaseException] = None

        def defer_signal(signum, _frame) -> None:
            deferred_signals.append(signum)

        for handled in handled_signals:
            signal.signal(handled, defer_signal)
        try:
            child = _spawn_child(args, request_transport, control_write_descriptor)
        except BaseException as error:
            spawn_error = error
        finally:
            _close_descriptor(control_write_descriptor)
            control_write_descriptor = -1
            _install_signal_handlers(handled_signals)
        if deferred_signals:
            raise GuardianInterrupted(deferred_signals[0])
        if spawn_error is not None:
            raise spawn_error
        child.communicate(input=task_snapshot)
        child_status = child.returncode
        if child_status is None:
            child_status = child.wait()
        exit_code = (
            128 + abs(child_status)
            if child_status < 0
            else child_status
        )
    except GuardianInterrupted as interrupted:
        exit_code = 128 + interrupted.signum
    except PreparationTimeout:
        print("guardian error: preparation timeout", file=sys.stderr)
        exit_code = 124
    except GuardianError:
        print("guardian error: invalid input", file=sys.stderr)
        exit_code = 2
    except Exception:
        print("guardian error: operation failed", file=sys.stderr)
        exit_code = 125
    finally:
        _ignore_signals(handled_signals)
        if child is not None:
            _terminate_child(child, control_read_descriptor)
        _close_descriptor(control_write_descriptor)
        _close_descriptor(control_read_descriptor)
        if request_transport is not None:
            request_transport.close()
        if directory_descriptor >= 0:
            if not _scrub_directory(directory_descriptor):
                exit_code = 125
            if (
                not directory_removed
                and parent_descriptor >= 0
                and directory_identity is not None
                and _remove_bound_directory(
                    parent_descriptor,
                    directory_basename,
                    directory_identity,
                )
            ):
                directory_removed = True
            os.close(directory_descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
        if input_dir is not None and not directory_removed:
            print("guardian error: cleanup binding failed", file=sys.stderr)
            exit_code = 125
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
