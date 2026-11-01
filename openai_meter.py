"""Расширяет существующие журналы: метаданные вызова и наблюдаемые HTTP-попытки, без секретов."""

import hashlib
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from billing_api import redact
from file_io import atomic_write_json, json_lock
from billing_units import consumption, billed_searches

DEFAULT_DIRECTORY = Path(__file__).parent / "outputs" / "openai_misc"
SCOPE_PATH = Path(__file__).parent / "outputs" / "billing" / "scope.json"


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_object(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def run_context(directory, title=None):
    research = read_object(directory / "research.json")
    run = read_object(directory / "run.json")
    origin = read_object(directory / "research_origin.json")
    source = origin.get("source_run_directory") or research.get("run_directory") or str(directory.resolve())
    return {"video_id": run.get("video_id") or "video_" + hashlib.sha256(source.encode()).hexdigest()[:20],
            "video_title": title or research.get("topic") or run.get("topic"), "run_id": run.get("run_id") or str(directory.resolve())}


class OpenAIMeter:
    def __init__(self, directory, stage, api_key, record=None, persist=None, title=None):
        self.directory = Path(directory) if directory else DEFAULT_DIRECTORY
        self.record = record if record is not None else {}
        self.api_key = api_key
        self.persist = persist
        self.telemetry = {"schema_version": 2, **run_context(self.directory, title),
            "call_id": "call_" + uuid.uuid4().hex, "stage": stage, "provider": "openai",
            "step": self.record.get("step") or stage, "api_key_id": None, "project_id": None, "batch": False,
            "api_key_fingerprint": hashlib.sha256(api_key.encode()).hexdigest(),
            "started_at_utc": now_iso(), "ended_at_utc": None, "duration_seconds": None,
            "status": "started", "attempts": [], "usage": None, "model": None, "service_tier": None,
            "provider_request_id": None, "provider_response_id": None, "tools": None,
            "tool_usage": None, "billed_web_requests": None, "units": None, "parameters": None, "created_at": None,
            "transport_attempts_observed": False, "error": None}
        self.record["telemetry"] = self.telemetry
        # Связь ключа устанавливает только успешная административная сверка, не догадка по имени.
        scope = read_object(SCOPE_PATH)
        self.telemetry["api_key_id"] = scope.get("key_fingerprints", {}).get(self.telemetry["api_key_fingerprint"])
        if self.telemetry["api_key_id"] in scope.get("api_key_ids", []):
            self.telemetry["project_id"] = scope.get("project_id")
        self._warned = False

    def save(self):
        try:
            if self.persist:
                self.persist()
            else:
                self.directory.mkdir(parents=True, exist_ok=True)
                path = self.directory / "openai_usage.json"
                with json_lock(path):
                    if path.exists():
                        journal = json.loads(path.read_text())
                    else:
                        journal = {"requests": []}
                    entries = journal["requests"]
                    entries[:] = [entry for entry in entries if entry.get("telemetry", {}).get("call_id") != self.telemetry["call_id"]]
                    entries.append(redact(self.record, (self.api_key,)))
                    atomic_write_json(path, journal)
        except Exception:
            # Аналитика не меняет успешность генерации даже при недоступном диске журнала.
            if not self._warned:
                print("Не удалось сохранить полную телеметрию OpenAI; расходы этого вызова могут остаться нераспределёнными.", file=sys.stderr)
                self._warned = True

    def on_request(self, request):
        self.telemetry["transport_attempts_observed"] = True
        if self.telemetry["attempts"] and self.telemetry["attempts"][-1]["ended_at_utc"] is None:
            self.telemetry["attempts"][-1]["ended_at_utc"] = now_iso()
        self.telemetry["attempts"].append({"attempt_number": len(self.telemetry["attempts"]) + 1,
            "started_at_utc": now_iso(), "ended_at_utc": None, "http_status": None,
            "provider_request_id": None, "usage": None, "tool_usage": None,
            "client_request_id": request.headers.get("x-client-request-id")})
        self.save()

    def on_response(self, response):
        self.telemetry["provider_request_id"] = response.headers.get("x-request-id") or self.telemetry.get("provider_request_id")
        if self.telemetry["attempts"]:
            self.telemetry["attempts"][-1].update(ended_at_utc=now_iso(), http_status=response.status_code,
                                                   provider_request_id=response.headers.get("x-request-id"))
            self.save()

    def processing_failed(self, error):
        """Успешный HTTP-ответ может оказаться непригодным; usage при этом не теряется."""
        self.telemetry.update(status="failed", error={"type": type(error).__name__, "details": "Ответ не прошёл проверку текущего этапа."})
        self.save()

    def create(self, client, **body):
        started = time.monotonic()
        self.telemetry['configured_model']=body.get('model')
        self.telemetry['parameters']={name:body[name] for name in ['model','service_tier','reasoning','tools','max_retries'] if name in body}
        hooks = getattr(getattr(client, "_client", None), "event_hooks", None)
        installed = isinstance(hooks, dict)
        self.save()
        if installed:
            hooks.setdefault("request", []).append(self.on_request)
            hooks.setdefault("response", []).append(self.on_response)
        try:
            response = client.responses.create(**body)
            raw = response.model_dump(mode="json")
            tools = [item for item in raw.get("output", []) if item.get("type", "").endswith("_call")]
            self.telemetry.update(status=raw.get("status"), usage=raw.get("usage"), model=raw.get("model"),
                service_tier=raw.get("service_tier"), provider_response_id=raw.get("id"),
                provider_request_id=getattr(response, "_request_id", None), tools=tools,
                created_at=raw.get("created_at"), tool_usage=raw.get("tool_usage"),
                billed_web_requests=billed_searches(raw.get("tool_usage")), units=consumption(raw.get("usage")))
            if self.telemetry["attempts"]:
                self.telemetry["attempts"][-1].update(usage=raw.get("usage"),tool_usage=raw.get("tool_usage"),
                    provider_response_id=raw.get("id"),model=raw.get("model"),service_tier=raw.get("service_tier"))
            return response
        except Exception as error:
            self.telemetry.update(status="failed", error={"type": type(error).__name__,
                "http_status": getattr(error, "status_code", None),
                "details": redact(getattr(error, "body", None), (self.api_key,))})
            self.telemetry["provider_request_id"] = getattr(error, "request_id", None) or self.telemetry.get("provider_request_id")
            raise
        finally:
            if installed:
                for name, handler in (("request", self.on_request), ("response", self.on_response)):
                    if handler in hooks[name]:
                        hooks[name].remove(handler)
            self.telemetry.update(ended_at_utc=now_iso(), duration_seconds=round(time.monotonic() - started, 3))
            for attempt in self.telemetry["attempts"]:
                if attempt["ended_at_utc"] is None:
                    attempt["ended_at_utc"] = self.telemetry["ended_at_utc"]
                start = datetime.fromisoformat(attempt["started_at_utc"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(attempt["ended_at_utc"].replace("Z", "+00:00"))
                attempt["duration_seconds"] = (end - start).total_seconds()
            self.save()
