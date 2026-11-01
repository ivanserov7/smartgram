"""Локальные инструменты монтажа. PATH компьютера и другие программы не изменяются."""

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

FILTERS = ("subtitles", "ass", "zoompan", "xfade", "loudnorm", "amix", "sidechaincompress")
ENCODERS = ("libx264", "aac")
SUBTITLE_TEXT = "Проверка субтитров: ёж, спутник, Земля"


class MediaError(Exception):
    """Понятная ошибка локальной проверки вместо технического traceback."""


def run_command(arguments, cwd=None, timeout=180, journal=None):
    """Никакой оболочки или API: только явные аргументы локального процесса."""
    started = time.monotonic()
    try:
        result = subprocess.run([str(value) for value in arguments], cwd=cwd, timeout=timeout,
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as error:
        if journal is not None:
            journal.append({'arguments':[str(value) for value in arguments], 'returncode':None,
                            'error':type(error).__name__, 'duration_seconds':time.monotonic()-started, 'executor':'local'})
        raise MediaError(f"Не удалось выполнить {Path(arguments[0]).name}: {type(error).__name__}.") from None
    if journal is not None:
        journal.append({"arguments": [str(value) for value in arguments], "returncode": result.returncode,
                        "stdout": result.stdout, "stderr": result.stderr,
                        "duration_seconds": time.monotonic()-started, "executor": "local"})
    if result.returncode:
        raise MediaError(f"{Path(arguments[0]).name}: код {result.returncode}. {result.stderr[-3000:].strip()}")
    return result.stdout, result.stderr


def executable(value):
    found = shutil.which(str(value))
    if not found:
        raise MediaError(f"Исполняемый файл недоступен: {value}.")
    return Path(found).absolute()


def find_brew():
    for value in (shutil.which("brew"), "/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
        if value and Path(value).is_file() and os.access(value, os.X_OK):
            return Path(value)
    return None


def brew_prefix(brew, formula):
    output, _ = run_command([brew, "--prefix", formula], timeout=30)
    return Path(output.strip())


@dataclass(frozen=True)
class MediaSettings:
    ffmpeg: Path
    ffprobe: Path
    font: Path
    font_query: Path


def load_media_settings(env_path=None):
    values = {**dotenv_values(env_path or Path(__file__).with_name(".env"), interpolate=False), **os.environ}
    ffmpeg, ffprobe = values.get("FFMPEG_PATH"), values.get("FFPROBE_PATH")
    brew = find_brew()
    if bool(ffmpeg) != bool(ffprobe):
        raise MediaError("Укажите вместе FFMPEG_PATH и FFPROBE_PATH в локальном .env.")
    if not ffmpeg:
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            if brew is None:
                raise MediaError("FFmpeg/ffprobe и Homebrew не найдены. Установите Homebrew с brew.sh, затем brew install ffmpeg-full.")
            prefix = brew_prefix(brew, "ffmpeg-full")
            ffmpeg, ffprobe = prefix / "bin/ffmpeg", prefix / "bin/ffprobe"
    ffmpeg, ffprobe = executable(ffmpeg), executable(ffprobe)
    font = Path(values.get("SUBTITLE_FONT_PATH") or "/System/Library/Fonts/Supplemental/Arial.ttf")
    if not font.is_file():
        raise MediaError("Не найден SUBTITLE_FONT_PATH. Укажите существующий шрифт с кириллицей.")
    query = shutil.which("fc-query")
    if not query and brew:
        query = brew_prefix(brew, "fontconfig") / "bin/fc-query"
    if not query:
        raise MediaError("Не найден fc-query для проверки кириллицы; он входит в зависимость fontconfig сборки ffmpeg-full.")
    return MediaSettings(ffmpeg, ffprobe, font.absolute(), executable(query))


def require_features(filters, encoders):
    # В таблицах FFmpeg имя идёт после флагов, поэтому не принимаем совпадение в описании.
    names = lambda table: {line.split()[1] for line in table.splitlines() if len(line.split()) >= 2}
    missing = [f"фильтр {name}" for name in FILTERS if name not in names(filters)]
    missing += [f"кодер {name}" for name in ENCODERS if name not in names(encoders)]
    if missing:
        raise MediaError("Сборка FFmpeg не подходит: отсутствует " + ", ".join(missing) + ". Установите brew install ffmpeg-full.")


def require_cyrillic(charset):
    """fc-query возвращает Unicode-диапазоны; проверяем каждый символ, включая ё."""
    ranges = []
    try:
        for item in charset.split():
            endpoints = item.split("-")
            ranges.append((int(endpoints[0], 16), int(endpoints[-1], 16)))
    except ValueError:
        raise MediaError("fc-query вернул непонятную таблицу символов шрифта.") from None
    missing = sorted({character for character in SUBTITLE_TEXT if not character.isspace()
                      and not any(start <= ord(character) <= end for start, end in ranges)})
    if missing:
        raise MediaError("В шрифте нет символов: " + "".join(missing))


def inspect_tools(settings, journal):
    versions = {}
    for name in ("ffmpeg", "ffprobe"):
        output, _ = run_command([getattr(settings, name), "-version"], journal=journal)
        versions[name] = output.splitlines()[0]
    filters, _ = run_command([settings.ffmpeg, "-hide_banner", "-filters"], journal=journal)
    encoders, _ = run_command([settings.ffmpeg, "-hide_banner", "-encoders"], journal=journal)
    require_features(filters, encoders)
    charset, _ = run_command([settings.font_query, "--format=%{charset}", settings.font], journal=journal)
    require_cyrillic(charset)
    family, _ = run_command([settings.font_query, "--format=%{family[0]}", settings.font], journal=journal)
    if not family.strip() or any(character in family for character in "\r\n," ):
        raise MediaError("Неподдерживаемое имя семейства шрифта для ASS.")
    return {"versions": versions, "filters": list(FILTERS), "encoders": list(ENCODERS),
            "font_family": family.strip(), "font_path": str(settings.font), "cyrillic_coverage": True}
