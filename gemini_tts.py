"""Настройки и один вызов официального Google GenAI SDK для озвучки."""

import base64
import binascii
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx
from dotenv import dotenv_values
from google import genai
from google.genai import errors, types
from google.genai._interactions import APIError as InteractionAPIError
from google.genai._interactions import APIConnectionError as InteractionConnectionError


DEFAULT_STYLE = "Спокойный познавательный рассказ на русском, чёткая дикция, естественный темп."


from voice_errors import VoiceoverError


@dataclass(frozen=True)
class VoiceSettings:
    """Одинаковые параметры для всех сцен; ключ сюда не входит."""

    model: str = "gemini-3.8-flash-tts"
    voice: str = "Kore"
    style: str = DEFAULT_STYLE

    def synthesis_options(self) -> dict:
        """Все параметры, влияющие на переиспользование звука."""
        return {**asdict(self), "api": "interactions-v1beta", "request_version": 1,
                "format": "audio/wav", "sample_rate": 24000, "channels": 1, "sample_width": 2}


def load_voice_settings() -> tuple[str, VoiceSettings]:
    """Читает только Google-настройки. OpenAI-ключ для озвучки не нужен."""
    values = dotenv_values(Path(__file__).with_name(".env"), interpolate=False)
    api_key = (values.get("GEMINI_API_KEY") or "").strip()
    if not api_key or api_key in {"your_google_api_key_here", "your_api_key_here"}:
        raise VoiceoverError("Добавьте GEMINI_API_KEY в .env. Для проверки голоса ключ OpenAI не нужен.")
    defaults = VoiceSettings()
    settings = {}
    for field, name in (("model", "GEMINI_TTS_MODEL"), ("voice", "GEMINI_TTS_VOICE"), ("style", "GEMINI_TTS_STYLE")):
        value = values.get(name, getattr(defaults, field))
        if not isinstance(value, str) or not value.strip():
            raise VoiceoverError(f"Укажите непустое значение {name} в .env.")
        settings[field] = value.strip()
    return api_key, VoiceSettings(**settings)


def api_error_message(status) -> str:
    """Сообщения зависят только от HTTP-кода, не от потенциально секретного тела ответа."""
    messages = {
        400: "Gemini отклонил запрос. Проверьте модель TTS и голос в .env.",
        401: "Gemini отклонил ключ. Проверьте GEMINI_API_KEY в .env.",
        403: "Доступ Gemini запрещён. Проверьте ключ и доступ аккаунта.",
        404: "Модель или голос Gemini недоступны. Проверьте настройки .env.",
        429: "Исчерпана квота или лимит Gemini. Проверьте баланс и лимиты.",
    }
    return messages.get(status, "Ошибка сервиса Gemini. Автоматический повтор отключён.")


def single_request_client() -> httpx.Client:
    """Запрещает второй HTTP-запрос независимо от особенностей повторов SDK."""
    sent = False

    def before_request(request):
        nonlocal sent
        if sent:
            raise VoiceoverError("Повтор запроса Gemini заблокирован. Проверьте сеть и повторите запуск вручную.")
        sent = True

    def after_response(response):
        # Исключение нашего типа не запускает встроенные HTTP-повторы SDK.
        if response.is_error:
            raise VoiceoverError(api_error_message(response.status_code))

    return httpx.Client(timeout=120.0, follow_redirects=False,
                        event_hooks={"request": [before_request], "response": [after_response]})


def synthesize_wav(narration: str, settings: VoiceSettings, api_key: str) -> bytes:
    """Отправляет исходный текст без изменений; стиль передаётся отдельно."""
    try:
        with single_request_client() as http_client, genai.Client(
            api_key=api_key, vertexai=False,
            http_options=types.HttpOptions(
                api_version="v1beta", timeout=120_000,
                retry_options=types.HttpRetryOptions(attempts=0), httpx_client=http_client,
            ),
        ) as client:
            response = client.interactions.create(
                model=settings.model,
                input=[{"type": "user_input", "content": [{
                    "type": "text", "text": narration,
                    "annotations": [{"type": "speech_metadata", "style": settings.style}],
                }]}],
                generation_config={"speech_config": [{"voice": settings.voice}]},
                response_format={"type": "audio", "mime_type": "audio/wav", "sample_rate": 24000},
                stream=False, store=False, automatic_continuation=False,
            )
        if response.status != "completed" or response.output_audio is None:
            raise VoiceoverError("Gemini не вернул завершённую озвучку. Автоматический повтор отключён.")
        audio = response.output_audio
        if audio.mime_type not in {"audio/wav", "audio/x-wav"} or not audio.data:
            raise VoiceoverError("Gemini вернул неподдерживаемый или пустой аудиоответ.")
        return base64.b64decode(audio.data, validate=True)
    except errors.APIError as error:
        raise VoiceoverError(api_error_message(error.code)) from None
    except InteractionConnectionError:
        raise VoiceoverError("Не удалось связаться с Gemini: ошибка сети или таймаут. Повтора не будет.") from None
    except InteractionAPIError as error:
        # В SDK 2.34 Interactions использует отдельное семейство исключений.
        raise VoiceoverError(api_error_message(getattr(error, "status_code", None))) from None
    except (httpx.HTTPError, TimeoutError, OSError):
        raise VoiceoverError("Не удалось связаться с Gemini: ошибка сети или таймаут. Повтора не будет.") from None
    except (binascii.Error, ValueError, TypeError):
        raise VoiceoverError("Не удалось прочитать аудиоответ Gemini. Проверьте модель и версию google-genai.") from None
