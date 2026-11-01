"""Передаёт ZIP в существующую папку Google Drive для компьютера, без Drive API."""

import ctypes
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from dotenv import dotenv_values


DRIVE_FOLDER_ID = "13oz6ksdcwq8o63i67fKZcbOaOxGmoCtR"
MARKER_NAME = "smartgram_sync_target.json"
MAX_NAME_ATTEMPTS = 5


class ReviewSyncError(Exception):
    """Архив на рабочем столе готов, но передача в папку синхронизации не завершена."""


def configured_sync_directory(env_path: Path) -> Path | None:
    """Читает только путь; ключи API не используются и не выводятся."""
    values = dotenv_values(env_path, interpolate=False)
    value = (values.get("SMARTGRAM_REVIEW_SYNC_DIR") or "").strip()
    if not value:
        return None
    directory = Path(value)
    if not directory.is_absolute():
        raise ReviewSyncError("SMARTGRAM_REVIEW_SYNC_DIR должен быть абсолютным путём.")
    return directory


def validate_sync_directory(directory: Path) -> Path:
    """Не создаёт папку: проверяет существующее назначение и ID в маркере при каждой передаче."""
    if not directory.is_absolute() or not directory.is_dir():
        raise ReviewSyncError("Папка SMARTGRAM_REVIEW_SYNC_DIR недоступна. Проверьте Google Drive для компьютера.")
    marker = directory / MARKER_NAME
    try:
        if marker.is_symlink():
            raise ReviewSyncError("Файл-маркер не должен быть символической ссылкой.")
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ReviewSyncError(f"Не удалось прочитать {MARKER_NAME} в папке назначения.") from None
    if not isinstance(data, dict) or data.get("drive_folder_id") != DRIVE_FOLDER_ID:
        raise ReviewSyncError("drive_folder_id в маркере не совпадает с папкой Smartgram Review. Передача отменена.")
    return directory


def inspect_zip(path: Path) -> dict:
    """Проверяет ZIP полностью и считает размер и SHA-256, не загружая файл целиком в память."""
    try:
        with ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise ReviewSyncError("Проверка целостности ZIP не пройдена.")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
    except (BadZipFile, RuntimeError, ValueError, EOFError, zlib.error):
        raise ReviewSyncError("Файл не является исправным ZIP-архивом.") from None
    return {"size_bytes": size, "sha256": digest.hexdigest()}


def rename_new_file(source: Path, destination: Path) -> None:
    """Атомарно публикует новое имя, запрещая перезапись даже при совпадении в момент переименования."""
    if sys.platform == "darwin":
        # Обычный os.rename на macOS перезаписывает назначение. RENAME_EXCL запрещает это в самом ядре.
        rename = ctypes.CDLL(None, use_errno=True).renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(os.fsencode(source), os.fsencode(destination), 0x00000004) != 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number), str(destination))
    else:
        # Безопасный POSIX-вариант для тестов вне macOS: создание имени тоже не перезаписывает старый файл.
        os.link(source, destination)
        source.unlink()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def publish_archive(temporary: Path, directory: Path) -> tuple[Path, datetime]:
    """При коллизии ждёт следующую секунду UTC; старые версии никогда не заменяет."""
    for attempt in range(MAX_NAME_ATTEMPTS):
        created = utc_now().astimezone(timezone.utc).replace(microsecond=0)
        destination = directory / f"Smartgram_review_{created:%Y%m%dT%H%M%SZ}.zip"
        try:
            rename_new_file(temporary, destination)
            return destination, created
        except FileExistsError:
            if attempt + 1 < MAX_NAME_ATTEMPTS:
                time.sleep(1.0)
    raise ReviewSyncError("Не удалось выбрать свободное имя ZIP. Старые версии сохранены; latest.json не изменён.")


def update_latest(directory: Path, metadata: dict) -> None:
    """Заменяет latest.json только после полной записи. При сбое прежний указатель остаётся."""
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".latest_", suffix=".tmp", delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write((json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, directory / "latest.json")
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def sync_archive(archive_path: Path, directory: Path) -> dict:
    """Копирует только готовый ZIP и возвращает метаданные после успешного обновления latest.json."""
    directory = validate_sync_directory(directory)
    expected = inspect_zip(archive_path)
    temporary_path = None
    published_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".Smartgram_review_", suffix=".tmp", delete=False) as temporary:
            temporary_path = Path(temporary.name)
            with archive_path.open("rb") as source:
                shutil.copyfileobj(source, temporary, length=1024 * 1024)
            temporary.flush()
            os.fsync(temporary.fileno())
        if inspect_zip(temporary_path) != expected:
            raise ReviewSyncError("Размер или SHA-256 временной ZIP-копии не совпадает с исходным архивом.")
        published_path, created = publish_archive(temporary_path, directory)
        temporary_path = None
        if inspect_zip(published_path) != expected:
            raise ReviewSyncError("Размер или SHA-256 опубликованной ZIP-копии не совпадает с исходным архивом.")
        validate_sync_directory(directory)
        metadata = {"schema_version": 1, "archive_name": published_path.name,
                    "created_at_utc": created.isoformat().replace("+00:00", "Z"), **expected}
        update_latest(directory, metadata)
        return metadata
    except (OSError, ReviewSyncError) as error:
        detail = str(error) if isinstance(error, ReviewSyncError) else f"Ошибка файловой системы ({type(error).__name__})."
        if published_path is not None:
            detail += f" ZIP {published_path.name} уже опубликован локально, но latest.json не обновлён."
        raise ReviewSyncError(detail + " Предыдущие архивы и latest.json не заменялись.") from None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
