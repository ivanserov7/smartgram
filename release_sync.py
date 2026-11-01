"""Точечная передача принятых MP4 и компактного JSON в проверенную папку Drive."""

from datetime import datetime, timezone
from pathlib import Path
import os
import shutil
import tempfile
import json
from dotenv import dotenv_values
from billing_api import local_secret_values
from build_review_archive import SECRET_VALUE
from production_state import ROOT

from file_io import atomic_write_json
from production_state import read, sha
from review_archive_sync import (
    validate_sync_directory,
    rename_new_file,
    ReviewSyncError,
)


def sync_videos(status_file, directory):
    directory = validate_sync_directory(Path(directory))
    status_file = Path(status_file).resolve()
    status = read(status_file)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = directory / ("Smartgram_videos_" + stamp)
    # Папка версии не заменяет ни одну из прежних. Корень Drive не создаём.
    destination.mkdir(exist_ok=False)
    receipt = {
        "schema_version": 1,
        "directory": str(destination),
        "files": [],
        "cloud_upload_confirmed": False,
    }
    for entry in status["topics"]:
        if entry.get("status") != "ready":
            continue
        source = Path(entry["video"])
        if (
            sha(source) != entry["sha256"]
            or source.stat().st_size != entry["size_bytes"]
        ):
            raise ReviewSyncError("Готовый MP4 изменён до передачи.")
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
    compact = {
        "schema_version": 1,
        "status": status["status"],
        "topics": [
            {k: v for k, v in t.items() if k not in {"directory", "video"}}
            for t in status["topics"]
        ],
        "cloud_upload_confirmed": False,
    }
    text = json.dumps(compact, ensure_ascii=False)
    secrets = local_secret_values(
        dotenv_values(ROOT / ".env", interpolate=False), include_environment=True
    )
    if SECRET_VALUE.search(text) or any(value in text for value in secrets):
        raise ReviewSyncError(
            "В итоговом JSON обнаружен возможный секрет; JSON не передан."
        )
    atomic_write_json(destination / "assembly_status.json", compact)
    receipt["status_json_sha256"] = sha(destination / "assembly_status.json")
    return receipt
