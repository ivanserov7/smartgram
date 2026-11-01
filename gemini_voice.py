"""Рабочий Gemini TTS: весь текст, style отдельно, готовый WAV без второй обёртки."""

import base64
import binascii
import hashlib
import json
import re
import time
import uuid
from pathlib import Path

import httpx
from google import genai
from google.genai import types

from audio_files import inspect_wav
from billing_api import redact
from comparison_audio import read_audio
from file_io import atomic_write, atomic_write_json
from openai_meter import now_iso
from voice_api import VoiceAPI
from voice_errors import VoiceoverError
from gemini_transport import gemini_proxy_options


def synthesis_request(text, settings):
    """Один текстовый блок: управляющие слова не становятся произносимым текстом."""
    return {
        "model": settings.tts_model,
        "input": [
            {
                "type": "user_input",
                "content": [
                    {
                        "type": "text",
                        "text": text,
                        "annotations": [
                            {"type": "speech_metadata", "style": settings.tts_style}
                        ],
                    }
                ],
            }
        ],
        "generation_config": {"speech_config": [{"voice": settings.tts_voice}]},
        "response_format": {"type": "audio"},
        "stream": False,
        "store": False,
        "automatic_continuation": False,
    }


def retryable_response(status, body):
    message=json.dumps(body,ensure_ascii=False).casefold()
    permanent=any(word in message for word in (
        "billing","insufficient_quota","balance","payment","credits","limit: 0","limit is 0",
        "limit=0","perday","per_day","daily","billing_disabled","quota_exhausted"))
    permanent=permanent or bool(re.search(r'"(?:quota_?value|limit)"\s*:\s*"?0(?:\.0+)?"?(?=[,}\s])',message))
    if permanent:return False
    return status is not None and (status>=500 or status==429)


def network_failure(error):
    seen=set()
    for _ in range(6):
        if error is None or id(error) in seen:return False
        seen.add(id(error))
        if isinstance(error,(httpx.TransportError,TimeoutError)) or type(error).__name__ in {'APIConnectionError','APITimeoutError'}:return True
        error=error.__cause__ or error.__context__
    return False


def without_audio(value):
    """Большие base64-блоки находятся в исходном WAV, а не дублируются в журнале."""
    if isinstance(value, dict):
        return {
            key: (
                "[audio saved separately]"
                if key == "data" and isinstance(item, str) and len(item) > 256
                else without_audio(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [without_audio(item) for item in value]
    return value


class GeminiVoiceAPI(VoiceAPI):
    """Синтез Google; методы Deepgram и OpenAI проверки переиспользуются из VoiceAPI."""

    def save_record(self, record):
        path = self.directory / "gemini_usage.json"
        data = json.loads(path.read_text()) if path.exists() else {"requests": []}
        data["requests"] = [
            item for item in data["requests"] if item["call_id"] != record["call_id"]
        ] + [redact(record, self.keys.values())]
        atomic_write_json(path, data)

    def synthesize(self, script, attempt_directory):
        key = self.keys.get("GEMINI_API_KEY")
        if not key:
            raise VoiceoverError(
                "Нет GEMINI_API_KEY в .env. Получить ключ: https://aistudio.google.com/api-keys. Не присылайте его в чат."
            )
        proxy_options = gemini_proxy_options()
        request = synthesis_request(script["narration_text"], self.settings)
        record = {
            "call_id": "gemini_" + uuid.uuid4().hex,
            "provider": "gemini",
            "stage": "synthesize",
            "script_version_id": script["script_version_id"],
            "request": request,
            "model": self.settings.tts_model,
            "voice": self.settings.tts_voice,
            "style": self.settings.tts_style,
            "usage": None,
            "started_at_utc": now_iso(),
            "status": "started",
            "attempts": [],
            "http_request_count": 0,
            "transport_rounds": [],
            "sdk_retries": 0,
            "confirmed_cost": None,
        }
        self.save_record(record)
        for number in range(self.settings.tts_max_retries + 1):
            attempt=None
            logical={"number":number+1,"status":"started","started_at_utc":now_iso()}
            record['transport_rounds'].append(logical)
            raw_body=None;status=None;response_id=None;blocked_sdk_retry=False
            self.save_record(record)

            def on_request(request):
                nonlocal attempt,blocked_sdk_retry
                if attempt is not None:
                    blocked_sdk_retry=True
                    raise VoiceoverError('Скрытый повтор Gemini SDK заблокирован до отправки HTTP; повторами управляет конвейер.')
                attempt={'number':len(record['attempts'])+1,'transport_round':number+1,'started_at_utc':now_iso(),
                    'status':'started','usage':None,'method':request.method,'endpoint_path':request.url.path}
                record['attempts'].append(attempt);record['http_request_count']=len(record['attempts'])
                self.save_record(record)

            def on_response(response):
                nonlocal raw_body, status, response_id
                response.read()
                status = response.status_code
                response_id = response.headers.get(
                    "x-request-id"
                ) or response.headers.get("x-goog-request-id")
                try:
                    raw_body = response.json()
                except ValueError:
                    raw_body = {"message": "Ответ не содержит JSON."}
                attempt.update(
                    http_status=status,
                    request_id=response_id,
                    response=redact(without_audio(raw_body), self.keys.values()),
                )
                self.save_record(record)
                # SDK 2.34 mutates attempts=0 to 1, then Interactions interprets
                # that as one retry. Raise our non-transport error before the SDK
                # sees an HTTP error; only the outer loop may repeat the request.
                if response.is_error:
                    raise VoiceoverError(f"Gemini HTTP {status}: " + json.dumps(
                        redact(without_audio(raw_body),self.keys.values()),ensure_ascii=False))

            try:
                with httpx.Client(
                    **proxy_options,
                    timeout=self.settings.tts_timeout_seconds,
                    follow_redirects=False,
                    event_hooks={"request":[on_request],"response": [on_response]},
                ) as http_client:
                    with genai.Client(
                        api_key=key,
                        vertexai=False,
                        http_options=types.HttpOptions(
                            api_version="v1beta",
                            timeout=self.settings.tts_timeout_seconds * 1000,
                            retry_options=types.HttpRetryOptions(attempts=0),
                            httpx_client=http_client,
                        ),
                    ) as client:
                        response = client.interactions.create(**request)
                record["usage"] = (
                    response.usage.model_dump(mode="json")
                    if getattr(response, "usage", None)
                    else None
                )
                record["response_id"] = getattr(response, "id", None)
                record["request_id"] = response_id
                record["model"] = str(
                    getattr(response, "model", None) or self.settings.tts_model
                )
                attempt["usage"] = record["usage"]
                if response.status != "completed" or response.output_audio is None:
                    raise VoiceoverError(
                        "Gemini не завершил генерацию; возможно, достигнут лимит. Частичный ответ не одобрен."
                    )
                audio = response.output_audio
                mime = audio.mime_type
                if mime not in ("audio/wav", "audio/x-wav") or not audio.data:
                    raise VoiceoverError(
                        "Ожидался готовый audio/wav. Raw PCM не запрашивался и не трактуется по догадке."
                    )
                content = (
                    base64.b64decode(audio.data, validate=True)
                    if isinstance(audio.data, str)
                    else bytes(audio.data)
                )
                atomic_write(attempt_directory / "provider_audio.wav", content)
                measured = inspect_wav(content)
                _, samples, levels = read_audio(content)
                clipping = sum(abs(sample) >= 32767 for sample in samples)
                metadata = {
                    **measured,
                    "provider": "gemini",
                    "model": record["model"],
                    "configured_model": self.settings.tts_model,
                    "voice": self.settings.tts_voice,
                    "style": self.settings.tts_style,
                    "mime_type": mime,
                    "adapter_version": self.settings.tts_adapter_version,
                    "usage": record["usage"],
                    "request_id": response_id,
                    "response_id": record["response_id"],
                    "technical_checks": {
                        "passed": clipping == 0,
                        "sound_present": True,
                        "clipped_samples": clipping,
                        "peak_dbfs": levels["peak_dbfs"],
                        "rms_dbfs": levels["rms_dbfs"],
                        "container": "WAV",
                    },
                }
                attempt.update(status="completed", ended_at_utc=now_iso())
                logical.update(status='completed',ended_at_utc=now_iso())
                record.update(
                    status="completed",
                    audio_sha256=measured["audio_sha256"],
                    ended_at_utc=now_iso(),
                )
                if record["usage"]:
                    inputs = record["usage"].get("total_input_tokens")
                    outputs = record["usage"].get("total_output_tokens")
                    if inputs is not None and outputs is not None:
                        record["estimated_usd"] = (
                            inputs * 0.5 + outputs * 9
                        ) / 1_000_000
                        metadata["estimated_usd"] = record["estimated_usd"]
                self.save_record(record)
                return content, metadata
            except Exception as error:
                code = (
                    status
                    or getattr(error, "status_code", None)
                    or getattr(error, "code", None)
                )
                safe = redact(
                    (
                        raw_body
                        if raw_body is not None
                        else {"type": type(error).__name__}
                    ),
                    self.keys.values(),
                )
                message = (
                    str(error)
                    if isinstance(error, VoiceoverError)
                    else f"Gemini HTTP {code}: " + json.dumps(safe, ensure_ascii=False)
                )
                if attempt is not None:attempt.update(status="failed", ended_at_utc=now_iso(), error=message)
                logical.update(status='failed',ended_at_utc=now_iso(),error=message)
                record.update(status="failed", reason=message, ended_at_utc=now_iso())
                self.save_record(record)
                technical = not blocked_sdk_retry and (retryable_response(code,raw_body) or code is None and network_failure(error))
                if number < self.settings.tts_max_retries and technical:
                    time.sleep(min(2**number, 4))
                    continue
                raise VoiceoverError(
                    message + " Автоматической смены провайдера нет."
                ) from None

