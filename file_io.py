"""Атомарная запись файлов, общая для аудио и изображений."""

import fcntl
from contextlib import contextmanager
import os
import json
import tempfile
import hashlib
from pathlib import Path


def atomic_write(path: Path, content: bytes) -> None:
    """Заменяет файл после полной записи; при сбое прежний файл сохраняется."""
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def atomic_write_json(path: Path, data: dict) -> None:
    atomic_write(path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def atomic_copy(source, destination, *, expected_sha256=None):
    """Publish a complete file without following an existing destination symlink."""
    source, destination = Path(source), Path(destination)
    temporary_path = None
    checksum = hashlib.sha256()
    try:
        with source.open("rb") as original, tempfile.NamedTemporaryFile(
            dir=destination.parent, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            for chunk in iter(lambda: original.read(1024 * 1024), b""):
                temporary.write(chunk)
                checksum.update(chunk)
            temporary.flush()
            os.fsync(temporary.fileno())
        if expected_sha256 is not None and checksum.hexdigest() != expected_sha256:
            raise ValueError("Source changed during copy; destination retained.")
        temporary_path.replace(destination)
        return checksum.hexdigest()
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def validate_output_name(name):
    """Accept one human-readable filename stem, never a path or control text."""
    if (
        not isinstance(name, str)
        or not name.strip()
        or name in {".", ".."}
        or any(character in name for character in ("/", "\\"))
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
        or Path(name).name != name
    ):
        raise ValueError("Output name must be a nonempty filename, not a path.")
    return name


class LockBusyError(RuntimeError):
    """Another invocation owns this workflow's state and paid operations."""


@contextmanager
def exclusive_lock(path, message="Another invocation is already running."):
    """Acquire one workflow lock without waiting or sharing a stale state snapshot."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LockBusyError(message) from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def json_lock(path):
    """Serialize read-modify-write across threads/processes, not just file replacement."""
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.with_name('.'+path.name+'.lock').open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        try:yield
        finally:fcntl.flock(handle,fcntl.LOCK_UN)
