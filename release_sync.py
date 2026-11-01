"""Точечная передача принятых MP4 и компактного JSON в проверенную папку Drive."""

from datetime import datetime, timezone
from pathlib import Path
import os
import shutil
import tempfile
import json
import re
from dotenv import dotenv_values
from billing_api import local_secret_values
from build_review_archive import SECRET_VALUE
from production_state import ROOT

from file_io import atomic_write_json, validate_output_name
from production_state import read, sha
from review_archive_sync import (
    validate_sync_directory,
    rename_new_file,
    ReviewSyncError,
)


def prepare_release(status_file):
    """Проверяет весь манифест и источники до создания опубликованной версии."""
    try:
        status = read(status_file)
        if not isinstance(status, dict) or not isinstance(status.get("topics"), list):
            raise ValueError
        if not isinstance(status.get("status"), str):
            raise ValueError
        names = set()
        ready = []
        for entry in status["topics"]:
            if not isinstance(entry, dict):
                raise ValueError
            name = validate_output_name(entry.get("name"))
            if name in names:
                raise ReviewSyncError("Имена роликов в манифесте должны быть уникальными.")
            names.add(name)
            if entry.get("status") != "ready":
                continue
            source = Path(entry["video"])
            if not source.is_absolute():
                source = status_file.parent / source
            if source.is_symlink() or source.suffix.lower() != ".mp4" or not source.is_file():
                raise ReviewSyncError("Для передачи нужен обычный принятый MP4-файл.")
            expected = entry.get("sha256")
            if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise ValueError
            if type(entry.get("size_bytes")) is not int or entry["size_bytes"] <= 0:
                raise ValueError
            if sha(source) != expected or source.stat().st_size != entry["size_bytes"]:
                raise ReviewSyncError("Готовый MP4 изменён до передачи.")
            ready.append((entry, source))
        compact = {
            "schema_version": 1,
            "status": status["status"],
            "topics": [
                {k: v for k, v in entry.items() if k not in {"directory", "video"}}
                for entry in status["topics"]
            ],
            "cloud_upload_confirmed": False,
        }
        text = json.dumps(compact, ensure_ascii=False, allow_nan=False)
        secrets = local_secret_values(
            dotenv_values(ROOT / ".env", interpolate=False), include_environment=True
        )
        if SECRET_VALUE.search(text) or any(value in text for value in secrets):
            raise ReviewSyncError("В итоговом JSON обнаружен возможный секрет; передача отменена.")
        return ready, compact
    except (OSError, ValueError, TypeError, KeyError):
        raise ReviewSyncError("Некорректный манифест или недоступный MP4; передача отменена.") from None


def sync_videos(status_file, directory):
    directory = validate_sync_directory(Path(directory))
    ready, compact = prepare_release(Path(status_file).resolve())
    validate_sync_directory(directory)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # Эксклюзивное создание сохраняет версии и при одновременной передаче.
    destination = Path(tempfile.mkdtemp(prefix="Smartgram_videos_" + stamp + "_", dir=directory))
    receipt = {
        "schema_version": 1,
        "directory": str(destination),
        "files": [],
        "cloud_upload_confirmed": False,
    }
    for entry, source in ready:
        target = destination / (entry["name"] + ".mp4")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=destination, prefix=".", suffix=".tmp", delete=False
            ) as stream:
                temporary = Path(stream.name)
                with source.open("rb") as original:
                    shutil.copyfileobj(original, stream, 1024 * 1024)
                stream.flush()
                os.fsync(stream.fileno())
            if (
                sha(temporary) != entry["sha256"]
                or temporary.stat().st_size != entry["size_bytes"]
            ):
                raise ReviewSyncError("SHA/размер временной копии MP4 не совпадает.")
            validate_sync_directory(directory)
            rename_new_file(temporary, target)
            temporary = None
            if (
                sha(target) != entry["sha256"]
                or target.stat().st_size != entry["size_bytes"]
            ):
                raise ReviewSyncError(
                    "SHA/размер опубликованной копии MP4 не совпадает."
                )
            receipt["files"].append(
                {
                    "name": entry["name"],
                    "path": str(target),
                    "sha256": entry["sha256"],
                    "size_bytes": entry["size_bytes"],
                }
            )
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
    atomic_write_json(destination / "assembly_status.json", compact)
    receipt["status_json_sha256"] = sha(destination / "assembly_status.json")
    return receipt
