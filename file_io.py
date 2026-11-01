"""Атомарная запись файлов, общая для аудио и изображений."""

import fcntl
from contextlib import contextmanager
import os
import json
import tempfile
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


@contextmanager
def json_lock(path):
    """Serialize read-modify-write across threads/processes, not just file replacement."""
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.with_name('.'+path.name+'.lock').open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        try:yield
        finally:fcntl.flock(handle,fcntl.LOCK_UN)
