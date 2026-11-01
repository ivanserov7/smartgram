"""Явный выбор синтезатора. Смена сервиса при ошибке никогда не выполняется."""

from voice_errors import VoiceoverError


def create_voice_api(directory, settings, keys):
    if settings.tts_provider == "gemini":
        from gemini_voice import GeminiVoiceAPI

        return GeminiVoiceAPI(directory, settings, keys)
    if settings.tts_provider == "openai":
        from voice_api import VoiceAPI

        return VoiceAPI(directory, settings, keys)
    raise VoiceoverError("Неизвестный провайдер озвучки; fallback не выполнялся.")
