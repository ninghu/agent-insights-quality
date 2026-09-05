"""Small private JSON checkpoints. Completed files, not progress indexes, are truth.

The runner owns record contents (source/settings/date, retries, deadlines, remote
references) and scheduling. Persist an immutable run metadata record to bind a
resume, then use keys such as targets/weather/v0/attempts/01/turns/01.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any, BinaryIO

from .errors import QualityError
from .settings import production_runtime_root


class StateError(QualityError):
    """Public-safe local state failure."""


class CheckpointError(StateError):
    """Durability failed: the caller must stop further unsafe side effects."""

    def __init__(self) -> None:
        super().__init__("state_checkpoint_failed")


class StateConflict(StateError):
    def __init__(self) -> None:
        super().__init__("state_record_conflict")


_RESERVED = {"con", "prn", "aux", "nul"} | {
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
}


def _parts(key: str) -> tuple[str, ...]:
    if not isinstance(key, str) or len(key) > 512:
        raise StateError("state_path_invalid")
    parts = tuple(key.split("/"))
    if any(
        not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", part) or part in _RESERVED
        for part in parts
    ):
        raise StateError("state_path_invalid")
    return parts


def _inside(root: Path, path: Path) -> Path:
    """Reject symlink/junction escapes as well as lexical path traversal."""
    try:
        resolved = path.resolve()
        if (
            os.name == "nt" and resolved.drive.startswith("\\\\?\\")
            and not path.drive.startswith("\\\\?\\")
        ):
            # Windows realpath can retain this prefix when a missing parent is
            # created between its probes. Re-resolve once, never strip/trust it:
            # the same exact-path and containment checks must still succeed.
            resolved = path.resolve()
    except (OSError, RuntimeError) as error:
        raise StateError("state_path_invalid") from error
    if path.absolute() != resolved or not path.is_relative_to(root):
        raise StateError("state_path_invalid")
    return path


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _mkdir(path: Path) -> None:
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
        _sync_directory(directory.parent)


def _replace(source: Path, destination: Path) -> None:
    if os.name == "nt":
        # ReplaceFile permits snapshot readers; flush the replaced file too.
        # For the first save, use a write-through move instead.
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        replace = kernel.ReplaceFileW
        replace.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID,
        ]
        replace.restype = wintypes.BOOL
        if replace(str(destination), str(source), None, 0, None, None):
            _confirm_durable(destination)
            return
        error = ctypes.get_last_error()
        if error != 2:
            raise ctypes.WinError(error)
        move = kernel.MoveFileExW
        move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move.restype = wintypes.BOOL
        if not move(str(source), str(destination), 0x1 | 0x8):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(source, destination)
        _sync_directory(destination.parent)


def _encode(value: Mapping[str, Any]) -> bytes:
    def validate_keys(item: Any) -> None:
        if isinstance(item, Mapping):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("JSON object keys must be strings")
            for child in item.values():
                validate_keys(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                validate_keys(child)

    try:
        if not isinstance(value, Mapping):
            raise ValueError("Records must be JSON objects")
        validate_keys(value)
        return (
            json.dumps(dict(value), sort_keys=True, ensure_ascii=True, allow_nan=False)
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        raise StateError("state_record_invalid") from error


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate record field")
        value[key] = item
    return value


def _open_snapshot(path: Path) -> BinaryIO:
    if os.name != "nt":
        return path.open("rb")
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    # Include FILE_SHARE_DELETE: ordinary Python/CRT readers prevent Windows
    # from replacing their file. A snapshot reader must not block checkpoints.
    handle = create(str(path), 0x80000000, 0x1 | 0x2 | 0x4, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except OSError:
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        close(handle)
        raise
    return os.fdopen(descriptor, "rb")


def _read(path: Path, *, missing_ok: bool) -> dict[str, Any] | None:
    try:
        with _open_snapshot(path) as stream:
            value = json.load(stream, object_pairs_hook=_unique_object)
        if not isinstance(value, dict):
            raise ValueError("Record is not an object")
        _encode(value)
        return value
    except FileNotFoundError as error:
        if missing_ok:
            return None
        raise StateError("state_record_missing") from error
    except (ValueError, UnicodeError, RecursionError, StateError) as error:
        raise StateError("state_record_corrupt") from error
    except OSError as error:
        raise StateError("state_read_failed") from error


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = None
    try:
        _mkdir(path.parent)
        descriptor, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _replace(temporary, path)
        temporary = None
    except OSError as error:
        raise CheckpointError() from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as error:
                raise CheckpointError() from error


def _confirm_durable(path: Path) -> None:
    # A prior rename may have succeeded before its directory flush failed.
    # Identical replay must establish durability too, not only compare content.
    try:
        with path.open("r+b") as stream:
            os.fsync(stream.fileno())
        _sync_directory(path.parent)
    except OSError as error:
        raise CheckpointError() from error


def _lock(descriptor: int, *, unlock: bool = False) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        mode = msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK
        msvcrt.locking(descriptor, mode, 1)
    else:
        import fcntl

        mode = fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(descriptor, mode)


class RuntimeStore:
    """One nonblocking OS ownership lock per environment; reads never acquire it.

    ``root`` injection is for tests. Production callers omit it, and must not
    expose a working-directory/config/environment override in their CLI.
    """

    def __init__(self, environment: str, *, root: Path | None = None) -> None:
        if environment not in ("daily", "staging"):
            raise StateError("state_environment_invalid")
        self.root = (production_runtime_root() if root is None else root).absolute()
        _inside(self.root, self.root)
        self.directory = self.root / "runner-v1" / environment
        self.environment = environment
        self._owned = False
        self._ownership_lock = threading.Lock()
        self._write_lock = threading.RLock()
        _inside(self.root, self.directory)

    @contextmanager
    def ownership(self) -> Iterator[RuntimeStore]:
        if not self._ownership_lock.acquire(blocking=False):
            raise StateError("state_owned")
        descriptor = None
        acquired = False
        try:
            path = _inside(self.root, self.directory / "ownership.lock")
            try:
                _mkdir(path.parent)
                descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"0")
                    os.fsync(descriptor)
            except OSError as error:
                raise CheckpointError() from error
            try:
                _lock(descriptor)
                acquired = True
            except OSError as error:
                if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise StateError("state_owned") from error
                raise CheckpointError() from error
            self._owned = True
            yield self
        finally:
            with self._write_lock:
                self._owned = False
                try:
                    if descriptor is not None:
                        try:
                            if acquired:
                                _lock(descriptor, unlock=True)
                        finally:
                            os.close(descriptor)
                finally:
                    self._ownership_lock.release()

    def run(self, run_id: str) -> RecordStore:
        if len(_parts(run_id)) != 1:
            raise StateError("state_path_invalid")
        return RecordStore(self, self.directory / "runs" / run_id)

    @property
    def staging_index(self) -> RecordStore:
        if self.environment != "staging":
            raise StateError("state_environment_invalid")
        return RecordStore(self, self.directory / "last-tests")

    def outbox(self, name: str) -> RecordStore:
        if len(_parts(name)) != 1:
            raise StateError("state_path_invalid")
        return RecordStore(self, self.directory / "outboxes" / name)


class RecordStore:
    def __init__(self, runtime: RuntimeStore, directory: Path) -> None:
        self._runtime = runtime
        self.directory = _inside(runtime.directory, directory)

    def _path(self, collection: str, key: str) -> Path:
        parts = _parts(key)
        path = self.directory / collection / Path(*parts[:-1]) / (parts[-1] + ".json")
        return _inside(self._runtime.root, path)

    def read(self, key: str, *, missing_ok: bool = False) -> dict[str, Any] | None:
        value = self.read_completed(key, missing_ok=True)
        if value is not None:
            return value
        return _read(self._path("progress", key), missing_ok=missing_ok)

    def read_completed(
        self, key: str, *, missing_ok: bool = False
    ) -> dict[str, Any] | None:
        return _read(self._path("completed", key), missing_ok=missing_ok)

    def save_progress(self, key: str, value: Mapping[str, Any]) -> None:
        self._save("progress", key, value)

    def save_completed(self, key: str, value: Mapping[str, Any]) -> None:
        self._save("completed", key, value)

    def save_artifact(self, key: str, value: Mapping[str, Any]) -> None:
        """Save immutable private JSON evidence before completing its stage."""
        self._save("artifacts", key, value)

    def read_artifact(
        self, key: str, *, missing_ok: bool = False
    ) -> dict[str, Any] | None:
        return _read(self._path("artifacts", key), missing_ok=missing_ok)

    def _save(self, collection: str, key: str, value: Mapping[str, Any]) -> None:
        content = _encode(value)
        with self._runtime._write_lock:
            if not self._runtime._owned:
                raise StateError("state_not_owned")
            path = self._path(collection, key)
            authoritative = (
                path if collection == "artifacts" else self._path("completed", key)
            )
            existing = _read(authoritative, missing_ok=True)
            if existing is not None:
                if _encode(existing) != content:
                    raise StateConflict()
                _confirm_durable(authoritative)
                return
            if collection == "progress":
                _read(path, missing_ok=True)
            _atomic_write(path, content)
