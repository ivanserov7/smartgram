"""Копирует готовые HTML-отчёты в проверенную папку Google Drive, без Drive API."""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from dotenv import dotenv_values

from build_review_archive import SECRET_VALUE
from billing_api import local_secret_values
from file_io import atomic_write_json
from review_archive_sync import (
    ReviewSyncError, configured_sync_directory, rename_new_file,
    utc_now, validate_sync_directory,
)
from report_audio_safety import text_without_checked_audio
from report_image_safety import text_without_checked_images

ENV_PATH = Path(__file__).with_name(".env")
INDEX_NAME = "reports.json"


def fingerprint(content: bytes) -> dict:
    """Размер и хеш должны совпадать до и после публикации."""
    return {"size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def read_report(path: Path, secret_values=()) -> bytes:
    """Передаём только HTML, без ссылок на локальные файлы вместо самого отчёта."""
    if path.is_symlink() or path.suffix.lower() not in {".html", ".htm"}:
        raise ReviewSyncError("Нужен обычный HTML-файл, не символическая ссылка.")
    content = path.read_bytes()
    try:
        text = content.decode("utf-8")
    except UnicodeError:
        raise ReviewSyncError("HTML-отчёт должен быть в UTF-8.") from None
    if "<html" not in text.lower() and "<!doctype html" not in text.lower():
        raise ReviewSyncError("Файл не похож на HTML-отчёт.")
    # Точные значения секретов ищем и в исходном HTML. Для WAV дополнительно
    # проверяем декодированные байты: base64 PCM может случайно напоминать ключ AWS.
    checked_text = text_without_checked_audio(text, secret_values)
    checked_text = text_without_checked_images(checked_text, secret_values)
    if SECRET_VALUE.search(checked_text) or any(value and value in text for value in secret_values):
        raise ReviewSyncError("В отчёте обнаружен возможный секрет. Копирование отменено; содержимое не выводится.")
    return content


def read_index(directory: Path) -> dict:
    """Повреждённый список не сбрасываем: предыдущие записи должны сохраниться."""
    path = directory / INDEX_NAME
    if path.is_symlink():
        raise ReviewSyncError("reports.json не должен быть символической ссылкой.")
    if not path.exists():
        return {"schema_version": 1, "reports": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("reports"), list):
            raise ValueError
        for entry in data["reports"]:
            name = entry["report_name"]
            if not isinstance(name, str) or Path(name).name != name or not name.endswith(".html"):
                raise ValueError
            if not isinstance(entry["source_id"], str) or not isinstance(entry["size_bytes"], int):
                raise ValueError
            if not isinstance(entry["sha256"], str):
                raise ValueError
        return data
    except (ValueError, KeyError, TypeError):
        raise ReviewSyncError("Не удалось прочитать структуру reports.json. Предыдущий список не изменён.") from None


def publish_report(temporary: Path, directory: Path, label: str) -> tuple[Path, str]:
    """При совпадении добавляет номер; старые отчёты не перезаписывает."""
    created = utc_now()
    prefix = f"Smartgram_report_{created:%Y%m%dT%H%M%SZ}_{label}"
    for number in range(100):
        suffix = f"_{number}" if number else ""
        path = directory / f"{prefix}{suffix}.html"
        try:
            rename_new_file(temporary, path)
            return path, created.isoformat().replace("+00:00", "Z")
        except FileExistsError:
            continue
    raise ReviewSyncError("Не найдено свободное имя HTML. Старые отчёты сохранены.")


def sync_html_report(path: Path, directory: Path, secret_values=()) -> dict:
    """Публикует проверенную копию, затем атомарно дополняет список отчётов."""
    temporary_path = None
    published = None
    try:
        validate_sync_directory(directory)
        content = read_report(path, secret_values)
        expected = fingerprint(content)
        source_id = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
        index = read_index(directory)
        for entry in reversed(index["reports"]):
            if entry["source_id"] == source_id and all(entry[key] == value for key, value in expected.items()):
                existing = directory / entry["report_name"]
                if existing.is_file() and not existing.is_symlink() and fingerprint(existing.read_bytes()) == expected:
                    return {**entry, "reused": True}
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".Smartgram_report_", suffix=".tmp", delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        if fingerprint(temporary_path.read_bytes()) != expected:
            raise ReviewSyncError("Размер или SHA-256 временной HTML-копии не совпадает.")
        label = re.sub(r"[^A-Za-z0-9_-]+", "_", f"{path.parent.name}_{path.stem}")[:100]
        published, created = publish_report(temporary_path, directory, label)
        temporary_path = None
        if fingerprint(published.read_bytes()) != expected:
            raise ReviewSyncError("Размер или SHA-256 опубликованного HTML не совпадает.")
        validate_sync_directory(directory)
        metadata = {"report_name": published.name, "created_at_utc": created, "source_id": source_id, **expected}
        atomic_write_json(directory / INDEX_NAME, {
            "schema_version": 1, "latest_report_name": published.name,
            "reports": [*index["reports"], metadata],
        })
        return {**metadata, "reused": False}
    except (OSError, ReviewSyncError) as error:
        detail = str(error) if isinstance(error, ReviewSyncError) else f"Ошибка файловой системы ({type(error).__name__})."
        if published is not None:
            detail += f" Копия {published.name} опубликована локально, но reports.json не обновлён."
        raise ReviewSyncError(detail + " Исходный HTML и прежний список отчётов сохранены.") from None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def sync_generated_report(path: Path, *, refresh_billing=True) -> bool:
    """Ошибка передачи явно сообщается, но не удаляет результаты исследования."""
    try:
        # Работает и при недоступной папке Drive: аналитика останется локально.
        if refresh_billing:
            from billing_sync import refresh_after_report
            refresh_after_report()
        directory = configured_sync_directory(ENV_PATH)
        if directory is None:
            print("HTML сохранён локально. Автопередача не настроена: SMARTGRAM_REVIEW_SYNC_DIR пуст.")
            return False
        values = dotenv_values(ENV_PATH, interpolate=False)
        secrets = local_secret_values(values, include_environment=True)
        metadata = sync_html_report(path, directory, secrets)
    except (OSError, ReviewSyncError) as error:
        detail = str(error) if isinstance(error, ReviewSyncError) else "Не удалось прочитать локальные настройки."
        print(f"HTML не передан: {detail}", file=sys.stderr)
        return False
    print(f"HTML {'уже скопирован' if metadata['reused'] else 'скопирован'} и проверен локально: {directory / metadata['report_name']}")
    print(f"Размер: {metadata['size_bytes']} байт; SHA-256: {metadata['sha256']}")
    print("Список reports.json сохранён. Завершение облачной синхронизации не подтверждено.")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path, help="Готовый автономный HTML-отчёт")
    args = parser.parse_args()
    return 0 if sync_generated_report(args.report) else 1


if __name__ == "__main__":
    raise SystemExit(main())
