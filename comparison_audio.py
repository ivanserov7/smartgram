"""Измерение исходного PCM/WAV и копия с одним линейным усилением, без изменения темпа."""

import array
import hashlib
import io
import math
import sys
import wave

from file_io import atomic_write
from voice_errors import VoiceoverError


def pcm_wav(pcm, rate=24000, channels=1):
    if not pcm or len(pcm) % (2 * channels):
        raise VoiceoverError("Пустой или оборванный PCM16; завершённый WAV не создан.")
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(pcm)
    return output.getvalue()


def read_audio(content):
    try:
        with wave.open(io.BytesIO(content)) as audio:
            channels, width, rate = (
                audio.getnchannels(),
                audio.getsampwidth(),
                audio.getframerate(),
            )
            declared = audio.getnframes()
            if (
                width != 2
                or audio.getcomptype() != "NONE"
                or channels not in (1, 2)
                or not 8000 <= rate <= 96000
            ):
                raise VoiceoverError(
                    "Ожидался PCM16 WAV; исходный формат сохранён, частота не подменена."
                )
            pcm = audio.readframes(declared)
            # Speech API может вернуть потоковый WAV с неизвестной длиной в заголовке.
            streaming_header = declared >= 0x1FFFFFFF
            if not streaming_header and len(pcm) != declared * width * channels:
                raise VoiceoverError(
                    "WAV обрезан: заявленное число кадров больше полученного."
                )
    except (wave.Error, EOFError, ValueError):
        raise VoiceoverError(
            "Ответ не читается как WAV. Исходный ответ сохранён."
        ) from None
    if not pcm or len(pcm) % (2 * channels):
        raise VoiceoverError("Пустое/повреждённое аудио.")
    samples = array.array("h", pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    peak = max(abs(value) for value in samples) / 32768
    rms = math.sqrt(sum(value * value for value in samples) / len(samples)) / 32768
    if rms == 0:
        raise VoiceoverError("Модель вернула только тишину; это не успешная озвучка.")
    metadata = {
        "audio_sha256": hashlib.sha256(content).hexdigest(),
        "sample_rate": rate,
        "channels": channels,
        "sample_width": width,
        "frame_count": len(samples) // channels,
        "duration_seconds": len(samples) / channels / rate,
        "peak": peak,
        "peak_dbfs": 20 * math.log10(peak),
        "rms_dbfs": 20 * math.log10(rms),
        "streaming_header_rewrapped": streaming_header,
    }
    return pcm, samples, metadata


def save_audio(directory, content):
    """Ответ провайдера сохраняется неизменно; WAV для плеера имеет точный заголовок."""
    atomic_write(directory / "provider_response.wav", content)
    pcm, samples, metadata = read_audio(content)
    original = pcm_wav(pcm, metadata["sample_rate"], metadata["channels"])
    atomic_write(directory / "original.wav", original)
    metadata["audio_sha256"] = hashlib.sha256(original).hexdigest()
    metadata["provider_response_sha256"] = hashlib.sha256(content).hexdigest()
    return metadata


def normalize_samples(entries, root):
    """Общий RMS снижается при необходимости: линейное усиление без клиппинга."""
    readable = []
    for entry in entries:
        if entry.get("audio"):
            directory = root / entry["id"]
            _, samples, measured = read_audio((directory / "original.wav").read_bytes())
            if measured["audio_sha256"] != entry["audio"]["audio_sha256"]:
                raise VoiceoverError(
                    "Исходный образец изменился; нормализация остановлена."
                )
            readable.append((entry, directory, samples, measured))
    if not readable:
        return
    # Для всех один RMS, а не разные цели по громкости.
    target = min(
        10 ** (-20 / 20),
        *(
            10 ** (item[3]["rms_dbfs"] / 20) * 0.95 / item[3]["peak"]
            for item in readable
        )
    )
    for entry, directory, samples, measured in readable:
        gain = target / 10 ** (measured["rms_dbfs"] / 20)
        output = array.array("h", (round(value * gain) for value in samples))
        if sys.byteorder != "little":
            output.byteswap()
        content = pcm_wav(
            output.tobytes(), measured["sample_rate"], measured["channels"]
        )
        atomic_write(directory / "level_matched.wav", content)
        _, _, normalized = read_audio(content)
        entry["level_matched"] = {
            **normalized,
            "gain_db": 20 * math.log10(gain),
            "method": "Один линейный коэффициент; без лимитера, изменения темпа и частоты.",
        }
