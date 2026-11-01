"""Проверка WAV и атомарная запись аудио и JSON без внешних программ."""

import hashlib
import io
import wave

from voice_errors import VoiceoverError
from file_io import atomic_write, atomic_write_json


def inspect_wav(content: bytes) -> dict:
    """Измеряет длительность по реальным кадрам и отвергает обрезанный WAV."""
    try:
        with wave.open(io.BytesIO(content), "rb") as audio:
            frames = audio.getnframes()
            rate = audio.getframerate()
            channels = audio.getnchannels()
            width = audio.getsampwidth()
            if frames <= 0 or not 8000 <= rate <= 96000 or channels not in (1, 2) or width != 2 or audio.getcomptype() != "NONE":
                raise VoiceoverError("Ожидался непустой PCM16 WAV: 8–96 кГц, один или два канала. Частота не подменяется.")
            if len(audio.readframes(frames)) != frames * channels * width:
                raise VoiceoverError("WAV обрезан: не хватает аудиокадров.")
    except (wave.Error, EOFError, ValueError):
        raise VoiceoverError("Аудиофайл не является исправным PCM WAV.") from None
    return {"duration_seconds": frames / rate, "frame_count": frames, "sample_rate": rate, "channels": channels, "sample_width": width,
            "audio_sha256": hashlib.sha256(content).hexdigest()}


def demo_wav(narration: str, settings, api_key: str) -> bytes:
    """Только тестовая тишина 0,25 с. Это не речь и не имитация настоящей озвучки."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\x00\x00" * 6000)
    return buffer.getvalue()
