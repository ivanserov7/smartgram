"""Секреты проверяются в содержимом WAV, а не в случайных совпадениях его base64."""

import base64
import binascii
import io
import re
import struct
import wave

from build_review_archive import SECRET_VALUE
from review_archive_sync import ReviewSyncError

WAV_DATA_URL = re.compile(r'data:audio/wav;base64,([A-Za-z0-9+/=]+)(?=["\'])')


def validate_pcm_wav(content):
    """PCM WAV: fmt/data и необязательный ограниченный блок C2PA от Google."""
    if len(content) < 44 or content[:4] != b"RIFF" or content[8:12] != b"WAVE":
        raise ReviewSyncError(
            "Встроенное аудио не является обычным WAV; передача отменена."
        )
    if struct.unpack_from("<I", content, 4)[0] + 8 != len(content):
        raise ReviewSyncError(
            "У встроенного WAV неверная длина или лишние данные; передача отменена."
        )
    offset, chunks = 12, []
    while offset < len(content):
        if offset + 8 > len(content):
            raise ReviewSyncError("Повреждён заголовок блока встроенного WAV.")
        kind = content[offset : offset + 4]
        size = struct.unpack_from("<I", content, offset + 4)[0]
        if kind not in (b"fmt ", b"data", b"C2PA") or offset + 8 + size > len(content):
            raise ReviewSyncError(
                "Во встроенном WAV неизвестный блок/вложение или обрыв."
            )
        # Gemini возвращает C2PA после PCM. Не удаляем сведения происхождения.
        # Подлинность подписи здесь не проверяется; весь блок ниже сканируется
        # на секреты вместе с PCM. Произвольные JUNK/LIST/RIFF не разрешены.
        if kind == b"C2PA" and not 0 < size <= 65536:
            raise ReviewSyncError("Недопустимый размер блока C2PA встроенного WAV.")
        chunks.append(kind)
        offset += 8 + size + size % 2
    if chunks not in ([b"fmt ", b"data"], [b"fmt ", b"data", b"C2PA"]) or offset != len(
        content
    ):
        raise ReviewSyncError("Неожиданная структура блоков встроенного WAV.")
    try:
        with wave.open(io.BytesIO(content)) as audio:
            if (
                audio.getcomptype() != "NONE"
                or audio.getsampwidth() != 2
                or audio.getnchannels() not in (1, 2)
                or not 8000 <= audio.getframerate() <= 96000
                or audio.getnframes() <= 0
            ):
                raise ReviewSyncError(
                    "Встроенный WAV должен быть непустым PCM16 с корректной частотой."
                )
            if (
                len(audio.readframes(audio.getnframes()))
                != audio.getnframes() * audio.getnchannels() * 2
            ):
                raise ReviewSyncError("Встроенный WAV обрезан.")
    except (wave.Error, EOFError):
        raise ReviewSyncError("Повреждён встроенный WAV.") from None


def text_without_checked_audio(text, secret_values):
    """Только представление для сканера; сам HTML и все аудиобайты не меняются."""
    binary_pattern = re.compile(SECRET_VALUE.pattern.encode("ascii"))
    secrets = [value.encode("utf-8") for value in secret_values if value]

    def check_audio(match):
        try:
            content = base64.b64decode(match.group(1), validate=True)
        except (binascii.Error, ValueError):
            raise ReviewSyncError("Неверный base64 встроенного аудио.") from None
        validate_pcm_wav(content)
        if binary_pattern.search(content) or any(value in content for value in secrets):
            raise ReviewSyncError(
                "В содержимом встроенного WAV обнаружен возможный секрет; передача отменена."
            )
        return "data:audio/wav;base64,[PCM_WAV_CHECKED]"

    return WAV_DATA_URL.sub(check_audio, text)
