"""Разные API для отдельного сравнения голосов; транспортные повторы отключены."""

import base64
import hashlib
import json
import math
import struct
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Literal

import httpx
from openai import OpenAI
from websockets.sync.client import connect

from billing_api import redact
from comparison_audio import pcm_wav
from file_io import atomic_write, atomic_write_json
from model_api import StrictModel
from openai_meter import OpenAIMeter, now_iso
from voice_api import VoiceAPI
from voice_errors import VoiceoverError


class ComparisonError(VoiceoverError):
    def __init__(self, message, *, technical=False, status="error", details=None):
        super().__init__(message)
        self.technical, self.status, self.details = technical, status, details


class TextDifference(StrictModel):
    expected: str
    observed: str
    category: Literal["material_difference", "possible_asr_error", "uncertainty"]
    explanation: str


class TextReview(StrictModel):
    status: Literal["Совпадает", "Есть расхождения в расшифровке", "Неопределённость"]
    full_text_present: bool
    issues: list[TextDifference]


class ComparisonAPI:
    def __init__(self, root, key, settings, text, narrator, check_prompt):
        self.root, self.key, self.settings = root, key, settings
        self.text, self.narrator, self.check_prompt = text, narrator, check_prompt

    def meter(self, directory, stage, model, request):
        meter = OpenAIMeter(
            directory,
            stage,
            self.key,
            record={
                "provider": "openai",
                "stage": stage,
                "model": model,
                "request": request,
                "money_charged": None,
            },
            title="Эксперимент русской озвучки GPS",
        )
        meter.telemetry.update(configured_model=model, model=model)
        return meter

    def http(self, directory, stage, endpoint, model, *, body=None, file=None):
        request = {"endpoint": endpoint, "body": body}
        if file:
            request["file_sha256"] = hashlib.sha256(file.read_bytes()).hexdigest()
        meter = self.meter(directory, stage, model, request)
        start = time.monotonic()
        meter.telemetry.update(
            transport_attempts_observed=True,
            attempts=[{"attempt_number": 1, "started_at_utc": now_iso()}],
        )
        meter.save()
        try:
            with httpx.Client(timeout=120, follow_redirects=False) as client:
                arguments = (
                    {"json": body}
                    if file is None
                    else {
                        "data": body,
                        "files": {
                            "file": ("audio.wav", file.read_bytes(), "audio/wav")
                        },
                    }
                )
                response = client.post(
                    "https://api.openai.com/v1/" + endpoint,
                    headers={"Authorization": "Bearer " + self.key},
                    **arguments,
                )
            request_id = response.headers.get("x-request-id")
            meter.telemetry.update(provider_request_id=request_id)
            meter.telemetry["attempts"][0].update(
                http_status=response.status_code, provider_request_id=request_id
            )
            if response.status_code != 200:
                try:
                    error = response.json()
                except ValueError:
                    error = {"message": response.text[:2000]}
                error = redact(error, (self.key,))
                meter.record["response"] = error
                raise ComparisonError(
                    f"HTTP {response.status_code}: "
                    + json.dumps(error, ensure_ascii=False),
                    technical=response.status_code >= 500,
                    status=(
                        "unavailable"
                        if response.status_code in (400, 401, 403, 404, 429)
                        else "error"
                    ),
                    details=error,
                )
            if endpoint == "audio/speech":
                raw = {
                    "response_sha256": hashlib.sha256(response.content).hexdigest(),
                    "size_bytes": len(response.content),
                    "usage": None,
                }
                value = response.content
            else:
                value = response.json()
                raw = json.loads(json.dumps(value))
                if endpoint == "chat/completions":
                    audio = raw["choices"][0]["message"].get("audio")
                    if audio and "data" in audio:
                        audio["data"] = "[сохранено в provider_response.wav]"
            meter.record["response"] = raw
            meter.telemetry.update(
                status="completed",
                model=raw.get("model") or model,
                usage=raw.get("usage"),
                provider_response_id=raw.get("id"),
                service_tier=raw.get("service_tier"),
            )
            meter.telemetry["attempts"][0]["usage"] = raw.get("usage")
            return value, {
                "request_id": request_id,
                "response_id": raw.get("id"),
                "model": raw.get("model") or model,
                "usage": raw.get("usage"),
                "request": request,
            }
        except httpx.HTTPError as error:
            meter.processing_failed(error)
            raise ComparisonError(
                f"{type(error).__name__}: транспортный сбой, результат запроса может быть неизвестен.",
                technical=True,
            ) from None
        except Exception as error:
            meter.processing_failed(error)
            raise
        finally:
            meter.telemetry.update(
                ended_at_utc=now_iso(), duration_seconds=time.monotonic() - start
            )
            meter.telemetry["attempts"][0]["ended_at_utc"] = meter.telemetry[
                "ended_at_utc"
            ]
            meter.save()

    def generate(self, variant, directory):
        model, voice, kind = variant["model"], variant["voice"], variant["api"]
        if kind == "realtime":
            settings = replace(
                self.settings,
                tts_provider="openai",
                tts_api="realtime",
                tts_max_retries=0,
                tts_model=model,
                tts_voice=voice,
                tts_timeout_seconds=100,
                narrator_prompt_file=str(
                    Path(__file__).parent / "prompts/comparison_narrator.txt"
                ),
            )
            # Промпт фиксируется снимком, но не меняет производственные настройки.
            api = VoiceAPI(
                directory,
                settings,
                {"OPENAI_API_KEY": self.key, "DEEPGRAM_API_KEY": ""},
            )
            script = {
                "script_version_id": hashlib.sha256(self.text.encode()).hexdigest(),
                "narration_text": self.text,
            }
            try:
                return api.synthesize(
                    script,
                    directory,
                    max_output_tokens=4096,
                    speed=1.0,
                    reasoning_effort="low",
                )
            except VoiceoverError as error:
                message = str(error)
                unavailable = any(
                    f"HTTP {code}" in message for code in (400, 401, 403, 404, 429)
                )
                raise ComparisonError(
                    message,
                    status="unavailable" if unavailable else "error",
                    technical=not unavailable
                    and any(
                        word in message for word in ("Timeout", "Connection", "HTTP 5")
                    ),
                ) from None
        if kind == "chat":
            body = {
                "model": model,
                "modalities": ["text", "audio"],
                "audio": {"voice": voice, "format": "wav"},
                "messages": [
                    {"role": "system", "content": self.narrator},
                    {"role": "user", "content": self.text},
                ],
                "max_completion_tokens": 4096,
                "store": False,
            }
            raw, meta = self.http(
                directory, "comparison_generation", "chat/completions", model, body=body
            )
            if raw["choices"][0]["finish_reason"] != "stop":
                raise ComparisonError(
                    "Chat Completions не завершил полную генерацию: "
                    + str(raw["choices"][0]["finish_reason"])
                )
            return (
                base64.b64decode(
                    raw["choices"][0]["message"]["audio"]["data"], validate=True
                ),
                meta,
            )
        if kind == "speech":
            body = {
                "model": model,
                "voice": voice,
                "input": self.text,
                "response_format": "wav",
                "speed": 1.0,
            }
            if model != "tts-1-hd":
                body["instructions"] = self.narrator
            return self.http(
                directory, "comparison_generation", "audio/speech", model, body=body
            )
        return self.live(variant, directory)

    def transcribe(self, path, directory):
        return self.http(
            directory,
            "comparison_transcription",
            "audio/transcriptions",
            "gpt-transcribe",
            body={"model": "gpt-transcribe", "languages[]": "ru"},
            file=path,
        )

    def check(self, transcript, directory):
        body = {
            "model": "gpt-6-luna",
            "reasoning": {"effort": "low"},
            "max_output_tokens": 1000,
            "instructions": self.check_prompt,
            "input": json.dumps(
                {"original_text": self.text, "independent_transcript": transcript},
                ensure_ascii=False,
            ),
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "comparison_text_check",
                    "strict": True,
                    "schema": TextReview.model_json_schema(),
                }
            },
        }
        meter = self.meter(directory, "comparison_text_check", "gpt-6-luna", body)
        try:
            with OpenAI(api_key=self.key, timeout=90, max_retries=0) as client:
                response = meter.create(client, **body)
            if response.status != "completed":
                raise ComparisonError(
                    "Текстовая проверка не завершена; сохранён usage, автоматической переозвучки нет."
                )
            review = TextReview.model_validate_json(response.output_text).model_dump()
            if review["status"] not in (
                "Совпадает",
                "Есть расхождения в расшифровке",
                "Неопределённость",
            ):
                raise ComparisonError("Некорректный статус текстовой проверки.")
            if review["status"] == "Совпадает" and (
                review["issues"] or not review["full_text_present"]
            ):
                raise ComparisonError("Противоречивый контракт полноты текста.")
            return review, {
                "usage": response.usage.model_dump(),
                "model": response.model,
                "request_id": getattr(response, "_request_id", None),
            }
        except Exception as error:
            meter.processing_failed(error)
            if isinstance(error, VoiceoverError):
                raise
            raise ComparisonError(
                "Проверка текста: "
                + json.dumps(
                    redact(getattr(error, "body", None), (self.key,)),
                    ensure_ascii=False,
                )
                + f" ({type(error).__name__})"
            ) from None

    def live(self, variant, directory):
        # Live — собственный протокол, а не response.create из Realtime.
        start_event = {
            "type": "session.start",
            "event_id": "start_comparison",
            "session": {
                "model": variant["model"],
                "instructions": self.narrator,
                "audio": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "output": {"voice": variant["voice"]},
                },
                "delegation": {"type": "client"},
                "store": False,
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": self.text}],
                    }
                ],
            },
        }
        commentary = {
            "type": "session.commentary.append",
            "event_id": "read_text",
            "delegation_id": None,
            "content": self.text,
        }
        request = {
            "url": "wss://api.openai.com/v1/live/sessions",
            "start": start_event,
            "commentary": commentary,
            "input_audio": "Локальная PCM16-тишина, 24 кГц, моно, пакеты по 20 мс; микрофон не используется.",
            "close_policy": "2.5 секунды тишины после речи; максимум 90 секунд; затем ожидание session.closed.",
        }
        meter = self.meter(
            directory, "comparison_generation", variant["model"], request
        )
        started = time.monotonic()
        events, pcm, closed, got_speech = [], bytearray(), None, False
        stop = threading.Event()
        sender, send_error, close_sent = None, [], False
        last_voice, audio_seconds, silence_seconds = started, 0.0, 0.0
        meter.telemetry.update(
            transport_attempts_observed=True,
            attempts=[{"attempt_number": 1, "started_at_utc": now_iso()}],
        )
        meter.save()
        try:
            with connect(
                request["url"],
                additional_headers={"Authorization": "Bearer " + self.key},
                open_timeout=20,
                close_timeout=3,
                max_size=16 * 1024 * 1024,
            ) as connection:
                meter.telemetry["provider_request_id"] = (
                    connection.response.headers.get("x-request-id")
                )
                connection.send(json.dumps(start_event, ensure_ascii=False))

                def send_silence():
                    packet = json.dumps(
                        {
                            "type": "session.input_audio.append",
                            "audio": base64.b64encode(bytes(960)).decode(),
                        }
                    )
                    while not stop.wait(0.02):
                        try:
                            connection.send(packet)
                        except Exception as error:
                            send_error.append(type(error).__name__)
                            return

                while True:
                    elapsed = time.monotonic() - started
                    if elapsed > 100:
                        raise ComparisonError(
                            "Live не прислал session.closed после закрытия; успешное завершение не подтверждено."
                        )
                    try:
                        event = json.loads(connection.recv(timeout=0.5))
                    except TimeoutError:
                        event = None
                    if event:
                        kind = event["type"]
                        if kind == "session.output_audio.delta":
                            chunk = base64.b64decode(event["delta"], validate=True)
                            pcm.extend(chunk)
                            duration = len(chunk) / 48000
                            audio_seconds += duration
                            values = struct.unpack("<" + "h" * (len(chunk) // 2), chunk)
                            rms = math.sqrt(
                                sum(v * v for v in values) / max(1, len(values))
                            )
                            if rms > 100:
                                got_speech, last_voice, silence_seconds = (
                                    True,
                                    time.monotonic(),
                                    0.0,
                                )
                            else:
                                silence_seconds += duration
                            events.append(
                                {"type": kind, "bytes": len(chunk), "rms": rms}
                            )
                        else:
                            events.append(redact(event, (self.key,)))
                        if kind == "session.started":
                            meter.telemetry.update(
                                provider_response_id=event["session"]["id"],
                                model=event["session"]["model"],
                            )
                            sender = threading.Thread(target=send_silence, daemon=True)
                            sender.start()
                            connection.send(json.dumps(commentary, ensure_ascii=False))
                        elif kind == "session.usage.updated":
                            meter.telemetry["usage"] = event["usage"]
                        elif kind == "session.delegation.created":
                            raise ComparisonError(
                                "Live запросил backend вместо самостоятельного чтения; backend не запускался.",
                                status="unsupported_workflow",
                            )
                        elif kind == "session.closed":
                            closed = event
                            meter.telemetry["usage"] = event.get(
                                "usage"
                            ) or meter.telemetry.get("usage")
                            break
                        elif kind == "error":
                            raise ComparisonError(
                                json.dumps(
                                    redact(event.get("error"), (self.key,)),
                                    ensure_ascii=False,
                                ),
                                status="unavailable",
                            )
                    if not close_sent and (
                        (
                            got_speech
                            and silence_seconds >= 2.5
                            and time.monotonic() - last_voice >= 2.5
                        )
                        or elapsed >= 90
                    ):
                        if elapsed >= 90:
                            raise ComparisonError(
                                "Live достиг ограничения 90 с без подтверждённого окончания чтения.",
                                status="unsupported_workflow",
                            )
                        connection.send(
                            json.dumps(
                                {
                                    "type": "session.close",
                                    "event_id": "close_comparison",
                                }
                            )
                        )
                        close_sent = True
                    if send_error:
                        raise ComparisonError(
                            "Live: ошибка передачи локальной тишины " + send_error[0],
                            technical=True,
                        )
                stop.set()
                if sender:
                    sender.join(timeout=1)
            if (
                not close_sent
                or not closed
                or closed.get("reason") != "close_requested"
                or not got_speech
            ):
                raise ComparisonError(
                    "Live не подтвердил штатное закрытие с непустой речью."
                )
            meter.telemetry.update(status="completed")
            return pcm_wav(bytes(pcm)), {
                "model": variant["model"],
                "voice": variant["voice"],
                "usage": meter.telemetry.get("usage"),
                "session_id": meter.telemetry.get("provider_response_id"),
                "session_open_seconds": time.monotonic() - started,
                "backend_requests": 0,
                "backend_estimated_usd": 0,
                "request": request,
                "completion": "Закрытие по акустической тишине; полнота требует независимого ASR, события audio.done у Live нет.",
            }
        except Exception as error:
            meter.processing_failed(error)
            if pcm:
                atomic_write(directory / "partial.pcm", bytes(pcm))
            if isinstance(error, ComparisonError):
                raise
            response = getattr(error, "response", None)
            code = getattr(response, "status_code", None)
            detail = None
            if response is not None:
                try:
                    detail = json.loads(response.body.decode())
                except (ValueError, AttributeError, UnicodeError):
                    pass
            raise ComparisonError(
                f"Live: {type(error).__name__}, HTTP {code}; "
                + json.dumps(redact(detail, (self.key,)), ensure_ascii=False),
                status="unavailable" if code in (400, 401, 403, 404, 429) else "error",
                technical=code is not None and code >= 500,
                details=detail,
            ) from None
        finally:
            stop.set()
            if sender:
                sender.join(timeout=1)
            meter.telemetry.update(
                ended_at_utc=now_iso(), duration_seconds=time.monotonic() - started
            )
            meter.telemetry["attempts"][0].update(
                ended_at_utc=meter.telemetry["ended_at_utc"],
                usage=meter.telemetry.get("usage"),
            )
            meter.save()
            atomic_write_json(directory / "live_events.json", events)
