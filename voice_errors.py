"""Общие ошибки озвучки, не зависящие от поставщика."""

class VoiceoverError(Exception):
    """Этап остановлен; сохранённые результаты не означают одобрение."""


class AudioContractError(VoiceoverError):
    """Непригодная структура ответа или таймкоды, а не ошибка произнесённого текста."""


class VoiceServiceError(VoiceoverError):
    """Технический сбой допускает ограниченный повтор только той же операции."""

    def __init__(self, message, status=None, details=None, network=False):
        super().__init__(message)
        self.status = status
        text = str(details).lower()
        billing = any(word in text for word in ("insufficient_quota", "billing", "balance", "perday", "per_day", "limit: 0"))
        self.retryable = network or (status is not None and status >= 500) or (status == 429 and not billing)


class AudioAlignmentError(AudioContractError):
    """Text may be accepted while Deepgram word/fragment timing remains unusable."""

    def __init__(self, message, text_verified=False):
        super().__init__(message)
        self.text_verified = text_verified


class AudioTechnicalError(VoiceoverError):
    """Fresh checks of the current WAV failed independently of text approval."""
