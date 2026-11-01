"""OpenAI Realtime, Deepgram prerecorded и проверки OpenAI. Без транспортных повторов."""

import base64
import binascii
import io
import json
import time
import wave
from urllib.parse import urlencode
from pathlib import Path

import httpx
from openai import OpenAI
from pydantic import ValidationError
from websockets.sync.client import connect

from audio_files import inspect_wav
from billing_api import redact
from file_io import atomic_write, atomic_write_json
from openai_meter import OpenAIMeter, now_iso
from voice_alignment import VerifiedAudioCheck
from voice_errors import AudioContractError, VoiceoverError, VoiceServiceError


class VoiceAPI:
    def __init__(self, directory, settings, keys):
        self.directory, self.settings, self.keys = Path(directory), settings, keys
        self.snapshot = settings.snapshot()

    def meter(self, stage, script_id, audio_hash=None, provider="openai", request=None):
        record = {"provider": provider, "stage": stage, "script_version_id": script_id,
                  "audio_sha256": audio_hash, "request": request, "money_charged": None}
        persist = None
        if provider != "openai":
            def persist():
                path = self.directory / "deepgram_usage.json"
                data = json.loads(path.read_text()) if path.exists() else {"requests": []}
                entries = data["requests"]
                call_id = record["telemetry"]["call_id"]
                entries[:] = [item for item in entries if item["telemetry"]["call_id"] != call_id]
                entries.append(redact(record, self.keys.values()))
                atomic_write_json(path, data)
        meter = OpenAIMeter(self.directory, stage, self.keys["OPENAI_API_KEY"] if provider == "openai" else self.keys["DEEPGRAM_API_KEY"], record=record, persist=persist)
        meter.telemetry.update(provider=provider, script_version_id=script_id, audio_sha256=audio_hash)
        if provider != "openai":
            meter.telemetry.update(api_key_id=None, api_key_fingerprint=None)
        return meter

    def synthesize(self, script, attempt_directory, *, max_output_tokens=None, speed=None, reasoning_effort=None):
        settings = self.settings
        session = {"type": "realtime", "model": settings.tts_model, "output_modalities": ["audio"],
                   "instructions": self.snapshot["prompts"]["narrator"]["text"],
                   "audio": {"output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": settings.tts_voice}}}
        # Необязательные параметры отдельного эксперимента. Рабочие defaults прежние.
        if max_output_tokens is not None:
            session["max_output_tokens"] = max_output_tokens
        if speed is not None:
            session["audio"]["output"]["speed"] = speed
        if reasoning_effort is not None:
            session["reasoning"] = {"effort": reasoning_effort}
        item = {"type": "conversation.item.create", "item": {"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": script["narration_text"]}]}}
        request = {"session": session, "item": item, "response": {"output_modalities": ["audio"]}}
        meter = self.meter("synthesize", script["script_version_id"], request=request)
        meter.telemetry.update(configured_model=settings.tts_model, transport_attempts_observed=True,
            attempts=[{"attempt_number": 1, "started_at_utc": now_iso(), "usage": None}])
        meter.save()
        start = time.monotonic()
        deadline = start + settings.tts_timeout_seconds
        events, pcm, done, observed_model = [], bytearray(), None, None
        try:
            url = "wss://api.openai.com/v1/realtime?" + urlencode({"model": settings.tts_model})
            with connect(url, additional_headers={"Authorization": "Bearer " + self.keys["OPENAI_API_KEY"]},
                         open_timeout=settings.tts_timeout_seconds, close_timeout=5, max_size=16*1024*1024) as connection:
                connection.send(json.dumps({"type": "session.update", "session": session}))
                updated = False
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise VoiceoverError("Realtime: истёк общий тайм-аут; завершённого WAV нет.")
                    event = json.loads(connection.recv(timeout=remaining))
                    kind = event.get("type")
                    if kind == "response.output_audio.delta":
                        chunk = base64.b64decode(event["delta"], validate=True)
                        pcm.extend(chunk)
                        events.append({"type": kind, "response_id": event.get("response_id"), "bytes": len(chunk)})
                        if len(pcm) > 24000*2*600:
                            raise VoiceoverError("Realtime: превышен защитный размер аудио (10 минут).")
                    else:
                        # Аудиобайты не попадают в диагностический JSON.
                        events.append(redact(event, self.keys.values()))
                    if kind in {"session.created", "session.updated"}:
                        observed_model = event.get("session", {}).get("model", observed_model)
                    if kind == "session.updated" and not updated:
                        updated = True
                        connection.send(json.dumps(item))
                        connection.send(json.dumps({"type": "response.create", "response": {"output_modalities": ["audio"]}}))
                    if kind == "error":
                        raise VoiceoverError("Realtime: " + json.dumps(redact(event.get("error"), self.keys.values()), ensure_ascii=False))
                    if kind == "response.created":
                        meter.telemetry["provider_response_id"] = event.get("response", {}).get("id")
                    if kind == "response.done":
                        done = event["response"]
                        meter.telemetry.update(provider_response_id=done.get("id"), usage=done.get("usage"), model=observed_model,
                                               status=done.get("status"))
                        meter.telemetry["attempts"][0]["usage"] = done.get("usage")
                        if done.get("status") != "completed":
                            raise VoiceoverError("Realtime response.done: " + json.dumps(done.get("status_details") or done.get("status"), ensure_ascii=False))
                        break
            if not done or not pcm or len(pcm) % 2:
                raise VoiceoverError("Realtime не вернул полное непустое PCM16-аудио. WAV не одобрен.")
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as wav:
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000); wav.writeframes(pcm)
            content = buffer.getvalue()
            measured = inspect_wav(content)
            meter.telemetry["audio_sha256"] = measured["audio_sha256"]
            meter.record["audio_sha256"] = measured["audio_sha256"]
            return content, {**measured, "response_id": done.get("id"), "usage": done.get("usage"), "model": observed_model,
                             "configured_model": settings.tts_model, "voice": settings.tts_voice}
        except Exception as error:
            meter.processing_failed(error)
            if isinstance(error, VoiceoverError):
                raise
            rejected = getattr(error, "response", None)
            if rejected is not None and getattr(rejected, "status_code", None):
                try:
                    details = json.loads(rejected.body.decode("utf-8"))
                except (ValueError, AttributeError, UnicodeError):
                    details = {"message": "Ответ при открытии WebSocket не содержит JSON ошибки."}
                meter.telemetry["attempts"][0]["http_status"] = rejected.status_code
                raise VoiceoverError(f"Realtime HTTP {rejected.status_code}, model={settings.tts_model}: "
                                     + json.dumps(redact(details, self.keys.values()), ensure_ascii=False)) from None
            raise VoiceoverError(f"Realtime: {type(error).__name__}; соединение/ответ не завершены, автоматического повтора нет.") from None
        finally:
            meter.telemetry.update(ended_at_utc=now_iso(), duration_seconds=round(time.monotonic()-start, 3))
            meter.telemetry["attempts"][0]["ended_at_utc"] = meter.telemetry["ended_at_utc"]
            meter.save()
            if pcm and (done is None or done.get("status") != "completed"):
                # Незавершённый звук сохраняется для диагностики, но не как успешный WAV.
                atomic_write(attempt_directory / "partial.pcm", bytes(pcm))
            atomic_write_json(attempt_directory / "realtime_events.json", redact(events, self.keys.values()))

    def transcribe(self, audio_path, script_id, audio_hash):
        settings = self.settings
        parameters = {"model": settings.asr_model, "language": settings.asr_language,
                      "punctuate": str(settings.asr_punctuate).lower(), "smart_format": str(settings.asr_smart_format).lower()}
        return self.audio_post("deepgram", "transcribe", "https://api.deepgram.com/v1/listen", audio_path, script_id,
                               audio_hash, settings.asr_timeout_seconds, parameters=parameters)

    def confirm(self, audio_path, script_id, audio_hash):
        # Актуальный gpt-transcribe принимает languages, не language. Исходного текста/подсказок здесь нет.
        return self.audio_post("openai", "audio_confirm", "https://api.openai.com/v1/audio/transcriptions", audio_path,
                               script_id, audio_hash, self.settings.confirm_timeout_seconds,
                               data={"model": self.settings.confirm_model, "languages[]": self.settings.asr_language})

    def audio_post(self, provider, stage, url, audio_path, script_id, audio_hash, timeout, parameters=None, data=None):
        # Проверяем именно те байты, которые отправим, и не перечитываем файл перед POST.
        content = audio_path.read_bytes()
        measured = inspect_wav(content)
        if measured["audio_sha256"] != audio_hash:
            raise AudioContractError("WAV изменился перед отправкой ASR; запрос не отправлен.")
        request = {"url": url, "parameters": parameters, "data": data, "audio_sha256": audio_hash,
                   "size_bytes": len(content), "duration_seconds": measured["duration_seconds"]}
        meter = self.meter(stage, script_id, audio_hash, provider, request)
        meter.telemetry["configured_model"] = self.settings.asr_model if provider == "deepgram" else self.settings.confirm_model
        started = time.monotonic()
        meter.telemetry.update(transport_attempts_observed=True, attempts=[{"attempt_number": 1, "started_at_utc": now_iso(), "usage": None}])
        meter.save()
        try:
            with httpx.Client(timeout=timeout, follow_redirects=False) as client:
                if provider == "deepgram":
                    response = client.post(url, params=parameters, content=content,
                        headers={"Authorization": "Token " + self.keys["DEEPGRAM_API_KEY"], "Content-Type": "audio/wav"})
                else:
                    response = client.post(url, data=data, files={"file": ("audio.wav", content, "audio/wav")},
                        headers={"Authorization": "Bearer " + self.keys["OPENAI_API_KEY"]})
            raw = response.json()
            meter.telemetry.update(provider_request_id=response.headers.get("x-request-id") or raw.get("metadata", {}).get("request_id"),
                provider_response_id=raw.get("id"), usage=raw.get("usage"), model=raw.get("model"), status="completed" if response.status_code == 200 else "failed")
            meter.record["response"] = redact(raw, self.keys.values())
            meter.telemetry["attempts"][0].update(http_status=response.status_code, usage=raw.get("usage"))
            if response.status_code != 200:
                raise VoiceServiceError(f"{provider} HTTP {response.status_code}: " + json.dumps(redact(raw, self.keys.values()), ensure_ascii=False), status=response.status_code, details=raw)
            return raw
        except Exception as error:
            meter.processing_failed(error)
            if isinstance(error, VoiceoverError):
                raise
            raise VoiceServiceError(f"{provider}: {type(error).__name__}; ошибка соединения/JSON.", network=isinstance(error, httpx.HTTPError)) from None
        finally:
            meter.telemetry.update(ended_at_utc=now_iso(), duration_seconds=round(time.monotonic()-started, 3))
            meter.telemetry["attempts"][0].update(ended_at_utc=meter.telemetry["ended_at_utc"],
                                                duration_seconds=meter.telemetry["duration_seconds"])
            meter.save()

    def check(self, script, transcription, audio_hash, confirmation=None, validation_feedback=None):
        instructions = self.snapshot["prompts"]["check"]["text"] + '\nВерни только JSON, строго по заданной схеме. fragment_index начинается с 1.'
        data = {"fragments": [{"fragment_index": index, **fragment} for index, fragment in enumerate(script["fragments"], 1)],
                "deepgram": transcription, "openai_transcript": confirmation}
        if validation_feedback:
            data["validation_feedback"] = validation_feedback
            instructions += "\nПредыдущий JSON не прошёл локальную проверку. Пересмотри его по validation_feedback и исходным расшифровкам. Цитаты должны буквально находиться в нужном ASR; совпадающую с expected цитату нельзя считать подтверждением ошибки. Это исправление проверки, не запрос новой озвучки."
        body = {"model": self.settings.check_model, "reasoning": {"effort": self.settings.check_reasoning_effort},
                "instructions": instructions, "input": json.dumps(data, ensure_ascii=False), "store": False,
                "text": {"format": {"type": "json_schema", "name": "audio_check", "strict": True, "schema": VerifiedAudioCheck.model_json_schema()}}}
        meter = self.meter("audio_check", script["script_version_id"], audio_hash, request=body)
        try:
            with OpenAI(api_key=self.keys["OPENAI_API_KEY"], timeout=self.settings.check_timeout_seconds, max_retries=0) as client:
                response = meter.create(client, **body)
            if response.status != "completed" or not response.output_text:
                raise VoiceoverError("OpenAI не завершил проверку текста озвучки.")
            return VerifiedAudioCheck.model_validate_json(response.output_text).model_dump()
        except Exception as error:
            meter.processing_failed(error)
            if isinstance(error, VoiceoverError):
                raise
            if isinstance(error, ValidationError):
                raise AudioContractError("OpenAI вернул непригодный JSON/контракт проверки речи. Это не семантическое расхождение.") from None
            raise VoiceServiceError("Проверка речи: " + json.dumps(redact(getattr(error, "body", None), self.keys.values()), ensure_ascii=False)
                                 + f" ({type(error).__name__}).", status=getattr(error,"status_code",None), details=getattr(error,"body",None), network=type(error).__name__ in {"APIConnectionError", "APITimeoutError"}) from None
