"""Настройки нового блока речи. Переменные окружения приоритетнее локального .env."""

import hashlib
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from dotenv import dotenv_values
from voice_errors import VoiceoverError

DEFAULT_GEMINI_STYLE = (
    "Русскоязычный профессиональный диктор. Нейтральное литературное русское произношение, "
    "естественная разговорная интонация, правильные ударения и смысловые паузы. "
    "Спокойная, живая подача короткого научно-популярного видео. Нормальный темп без спешки "
    "и растягивания слов; без рекламного пафоса и театральности. GPS произносится “джи-пи-эс”"
)


@dataclass(frozen=True)
class VoiceoverSettings:
    tts_provider: str = "gemini"
    tts_api: str = "interactions"
    tts_model: str = "gemini-3.8-flash-tts"
    tts_voice: str = "Kore"
    tts_style: str = DEFAULT_GEMINI_STYLE
    tts_adapter_version: str = "gemini-interactions-wav-v1"
    tts_timeout_seconds: int = 180
    tts_max_retries: int = 1
    max_text_regenerations: int = 1
    narrator_prompt_file: str = "prompts/voice_narrator.txt"
    asr_provider: str = "deepgram"
    asr_model: str = "nova-2"
    asr_language: str = "ru"
    asr_punctuate: bool = True
    asr_smart_format: bool = False
    asr_timeout_seconds: int = 120
    asr_max_retries: int = 1
    check_model: str = "gpt-6-luna"
    check_reasoning_effort: str = "low"
    check_timeout_seconds: int = 120
    check_max_retries: int = 1
    check_prompt_file: str = "prompts/voice_check.txt"
    confirm_model: str = "gpt-transcribe"
    confirm_timeout_seconds: int = 120
    confirm_max_retries: int = 1
    timing_tolerance_seconds: float = 0.02

    def snapshot(self):
        prompts = {}
        for name in ("narrator", "check"):
            if name == "narrator" and self.tts_provider == "gemini":
                text = self.tts_style
                prompts[name] = {
                    "text": text,
                    "sha256": hashlib.sha256(text.encode()).hexdigest(),
                }
                continue
            path = Path(getattr(self, name + "_prompt_file"))
            if not path.is_absolute():
                path = Path(__file__).parent / path
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                raise VoiceoverError(
                    f"Не удалось прочитать VOICE_{name.upper()}_PROMPT_FILE."
                ) from None
            if not text.strip():
                raise VoiceoverError(f"Промпт {name} пуст.")
            prompts[name] = {
                "text": text,
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
            }
        audio_format = (
            {"container": "wav", "parameters_source": "actual_provider_wav"}
            if self.tts_provider == "gemini"
            else {
                "encoding": "pcm16",
                "sample_rate": 24000,
                "channels": 1,
                "sample_width": 2,
            }
        )
        return {
            "schema_version": 3,
            "settings": asdict(self),
            "prompts": prompts,
            "format": audio_format,
        }


def load_voiceover_settings(env_path=None, require_keys=True):
    values = {
        **dotenv_values(
            env_path or Path(__file__).with_name(".env"), interpolate=False
        ),
        **os.environ,
    }
    keys = {
        name: (values.get(name) or "").strip()
        for name in ("OPENAI_API_KEY", "DEEPGRAM_API_KEY", "GEMINI_API_KEY")
    }
    settings = {}
    for name, default in asdict(VoiceoverSettings()).items():
        value = values.get("VOICE_" + name.upper(), default)
        try:
            if isinstance(default, bool):
                if str(value).lower() not in {"true", "false"}:
                    raise ValueError
                value = str(value).lower() == "true"
            elif isinstance(default, int):
                value = int(value)
                if value < (
                    0 if "retries" in name or name == "max_text_regenerations" else 1
                ):
                    raise ValueError
            elif isinstance(default, float):
                value = float(value)
                if not 0 <= value <= 0.1:
                    raise ValueError
            else:
                value = str(value).strip()
                if not value:
                    raise ValueError
        except (TypeError, ValueError):
            raise VoiceoverError(
                f"Некорректная настройка VOICE_{name.upper()}."
            ) from None
        settings[name] = value
    expected_api = {"gemini": "interactions", "openai": "realtime"}.get(
        settings["tts_provider"]
    )
    if (
        not expected_api
        or settings["tts_api"] != expected_api
        or settings["asr_provider"] != "deepgram"
    ):
        raise VoiceoverError(
            "Несогласованные VOICE_TTS_PROVIDER/API или ASR_PROVIDER; скрытого fallback нет."
        )
    if any(
        settings[name] > 2
        for name in (
            "tts_max_retries",
            "asr_max_retries",
            "confirm_max_retries",
            "check_max_retries",
        )
    ):
        raise VoiceoverError(
            "VOICE_*_MAX_RETRIES должны быть 0–2. Повторы выполняет конвейер, SDK имеет max_retries=0."
        )
    if require_keys:
        required = ["OPENAI_API_KEY", "DEEPGRAM_API_KEY"] + (
            ["GEMINI_API_KEY"] if settings["tts_provider"] == "gemini" else []
        )
        missing = [
            name
            for name in required
            if not keys[name] or keys[name].startswith("your_")
        ]
        if missing:
            raise VoiceoverError(
                "Заполните "
                + ", ".join(missing)
                + " в локальном .env. Gemini: https://aistudio.google.com/api-keys. Ключи в чат не присылайте."
            )
    return keys, VoiceoverSettings(**settings)
