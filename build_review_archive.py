"""Собирает архив для ревью без окружения, результатов и секретов."""

import argparse
import os
import re
import sys
import tempfile
import time
import base64
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
from dotenv import dotenv_values

from review_archive_sync import ReviewSyncError, configured_sync_directory, sync_archive
from billing_api import local_secret_values

PROJECT_DIRECTORY = Path(__file__).resolve().parent
EXCLUDED_DIRECTORIES = {
    ".venv",
    "venv",
    ".git",
    "__pycache__",
    "outputs",
    "output",
    "tmp",
    "temp",
    "node_modules",
    "review",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
# Включаем только исходники, документацию и настройки проекта, не бинарные файлы.
ALLOWED_EXTENSIONS = {
    ".py",
    ".md",
    ".txt",
    ".toml",
    ".json",
    ".yaml",
    ".yml",
    ".html",
    ".css",
    ".js",
    ".mjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".sh",
    ".service",
    ".conf",
}
SECRET_NAME = re.compile(
    r"secret|credential|token|password|private|id_rsa|id_ed25519", re.I
)
SECRET_VALUE = re.compile(
    r"sk-[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{30,}|AKIA[A-Z0-9]{16}"
    r"|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
)


def collect_project_files() -> list[Path]:
    """Обходит проект, пропуская скрытые папки, ссылки и потенциальные секреты."""
    files = []
    for directory, folders, names in os.walk(PROJECT_DIRECTORY, followlinks=False):
        folders[:] = sorted(
            name
            for name in folders
            if name not in EXCLUDED_DIRECTORIES
            and (not name.startswith(".") or
                 (Path(directory) == PROJECT_DIRECTORY and name == ".github"))
            and not SECRET_NAME.search(name)
            and not (Path(directory) / name).is_symlink()
        )
        for name in sorted(names):
            path = Path(directory) / name
            if path.is_symlink() or SECRET_NAME.search(name):
                continue
            if name.startswith(".") and path not in (
                PROJECT_DIRECTORY / ".env.example",
                PROJECT_DIRECTORY / ".gitignore",
            ):
                continue
            if path.suffix not in ALLOWED_EXTENSIONS and name not in (
                ".env.example",
                ".gitignore",
            ):
                continue
            files.append(path)
    return files


def validate_env_example(text: str) -> None:
    """Разрешает новые настройки, но в секретных полях — только заглушки."""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise ValueError("Некорректная строка настроек в .env.example.")
        if value.startswith(('"', "'")):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError("Незакрытые кавычки в .env.example.")
            value = value[1:-1]
        if re.search(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", name):
            if value and not re.fullmatch(
                r"your_[a-z0-9_]+_here|example|changeme", value
            ):
                raise ValueError(
                    "В секретных полях .env.example разрешены только заглушки."
                )
        elif name in {
            "OPENAI_BILLING_PROJECT_ID",
            "OPENAI_BILLING_TIMEOUT_SECONDS",
            "OPENAI_BILLING_MAX_RETRIES",
        }:
            # Новые административные настройки в шаблоне намеренно пусты; код имеет безопасные defaults.
            if value:
                raise ValueError(
                    "Настройки биллинга в .env.example должны быть пустыми."
                )
        elif name == "SMARTGRAM_REVIEW_SYNC_DIR":
            if value:
                raise ValueError(
                    "SMARTGRAM_REVIEW_SYNC_DIR в .env.example должен быть пустым, без личного пути."
                )
        elif name == "GEMINI_HTTP_PROXY":
            if value:
                raise ValueError("GEMINI_HTTP_PROXY в .env.example должен быть пустым.")
        elif not value:
            raise ValueError("Укажите пример значения настройки в .env.example.")


def read_safe_content(path: Path, secrets=()) -> bytes:
    """Проверяет текст, не выводя содержимое возможного секрета."""
    content = path.read_bytes()
    text = content.decode("utf-8")
    if SECRET_VALUE.search(text) or any(secret in text for secret in secrets):
        raise ValueError(
            "Обнаружен возможный секрет. Архив не обновлён; проверьте файлы проекта."
        )
    if path == PROJECT_DIRECTORY / ".env.example":
        validate_env_example(text)
    return content


def curated_review_files(secrets):
    """Разрешён только небольшой пакет с явным списком выбранных JPEG."""
    import json
    from report_image_safety import text_without_checked_images

    root = PROJECT_DIRECTORY / "review"
    if not root.exists():
        return []
    if root.is_symlink():
        raise ValueError("Review directory must not be a link.")
    files = [root / name for name in ("summary.json", "visuals.json", "changes.txt")]
    visuals = json.loads(read_safe_content(root / "visuals.json", secrets))
    for item in visuals["shots"]:
        relative = item.get("preview")
        if not relative:
            continue
        path = root / relative
        if (
            Path(relative).parent != Path("selected_previews")
            or path.suffix != ".jpg"
            or path.is_symlink()
            or path.parent.is_symlink()
        ):
            raise ValueError("Unexpected review image.")
        files.append(path)
    files = list(dict.fromkeys(files))
    if (
        any(p.is_symlink() for p in files)
        or sum(p.stat().st_size for p in files) > 5 * 1024 * 1024
    ):
        raise ValueError("Review package is unsafe or too large.")
    for path in files:
        if path.suffix == ".jpg":
            content = path.read_bytes()
            try:
                text_without_checked_images(
                    '<img src="data:image/jpeg;base64,'
                    + base64.b64encode(content).decode()
                    + '">',
                    secrets,
                )
                from PIL import Image

                with Image.open(path) as image:
                    if max(image.size) > 768:
                        raise ValueError("Review thumbnail is too large.")
            except ReviewSyncError:
                raise ValueError(
                    "Review image failed the secret/container check."
                ) from None
        else:
            read_safe_content(path, secrets)
    return files


def build_archive(destination: Path) -> Path:
    """Сначала собирает и проверяет ZIP, затем атомарно заменяет старый архив."""
    files = collect_project_files()
    secrets = local_secret_values(
        dotenv_values(PROJECT_DIRECTORY / ".env", interpolate=False),
        include_environment=True,
    )
    files += curated_review_files(secrets)
    required = {
        "AGENTS.md",
        "main.py",
        "pipeline.py",
        "research.py",
        "README.md",
        "requirements.txt",
        ".env.example",
        "build_review_archive.py",
    }
    if not required.issubset(
        {path.relative_to(PROJECT_DIRECTORY).as_posix() for path in files}
    ):
        raise ValueError("Не найдены обязательные файлы проекта. Архив не обновлён.")
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, suffix=".zip", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
        with ZipFile(temporary_path, "w", compression=ZIP_DEFLATED) as archive:
            for path in files:
                archive.writestr(
                    "Smartgram/" + path.relative_to(PROJECT_DIRECTORY).as_posix(),
                    (
                        path.read_bytes()
                        if path.suffix == ".jpg"
                        else read_safe_content(path, secrets)
                    ),
                )
        with ZipFile(temporary_path) as archive:
            if archive.testzip() is not None:
                raise ValueError("Проверка ZIP не пройдена. Архив не обновлён.")
        temporary_path.replace(destination)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return destination


def main() -> int:
    """Позволяет выбрать другую папку для локальной проверки сборки."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path.home() / "Desktop" / "Smartgram_review.zip"
    )
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="Только локальная проверка архива, без передачи в Google Drive",
    )
    arguments = parser.parse_args()
    packaging_started = time.monotonic()
    try:
        destination = build_archive(arguments.output.expanduser().absolute())
    except (OSError, ValueError):
        print(
            "Архив не обновлён. Проверьте доступ к папке, наличие файлов и отсутствие секретов в исходниках.",
            file=sys.stderr,
        )
        return 1
    packaging_seconds = time.monotonic() - packaging_started
    print(f"Архив обновлён: {destination}")
    if arguments.no_sync:
        print("Передача отключена параметром --no-sync.")
        return 0
    try:
        sync_directory = configured_sync_directory(PROJECT_DIRECTORY / ".env")
        if sync_directory is None:
            print(
                "Автопередача не настроена: SMARTGRAM_REVIEW_SYNC_DIR пуст. Локальный архив сохранён."
            )
            return 0
        transfer_started = time.monotonic()
        metadata = sync_archive(destination, sync_directory)
        transfer_seconds = time.monotonic() - transfer_started
        from file_io import atomic_write_json

        receipt = PROJECT_DIRECTORY / "outputs" / "review_build_receipt.json"
        receipt.parent.mkdir(exist_ok=True)
        atomic_write_json(
            receipt,
            dict(
                metadata,
                packaging_seconds=packaging_seconds,
                transfer_seconds=transfer_seconds,
                cloud_upload_confirmed=False,
            ),
        )
    except (ReviewSyncError, OSError) as error:
        detail = (
            str(error)
            if isinstance(error, ReviewSyncError)
            else "Не удалось прочитать настройки или обратиться к папке передачи."
        )
        print(
            f"Передача не завершена: {detail}\nГотовый локальный архив сохранён: {destination}",
            file=sys.stderr,
        )
        return 1
    print(
        f"ZIP скопирован и проверен локально в папке Google Drive: {sync_directory / metadata['archive_name']}"
    )
    print(f"Размер: {metadata['size_bytes']} байт; SHA-256: {metadata['sha256']}")
    print(
        "latest.json обновлён. Завершение загрузки в облако этим скриптом не подтверждено."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
