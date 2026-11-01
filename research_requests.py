"""Два вида запросов Responses API. В журнале — точные тела, ответы и раскрытые веб-действия."""

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from openai import APIConnectionError, APIError, APIStatusError, OpenAI
from pydantic import ValidationError

from file_io import atomic_write_json
from research import ResearchError, require_complete_response
from research_config import load_research_settings
from research_models import CollectionResult, SufficiencyResult
from openai_meter import OpenAIMeter


PROMPT_DIRECTORY = Path(__file__).with_name("prompts")
FORMAT_INSTRUCTIONS = "\n\nВерни только JSON по заданной JSON Schema. Не добавляй Markdown вокруг JSON. Неизвестные nullable-поля верни как null, не придумывай значения."


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def web_metadata(response: dict) -> dict:
    calls, citations = [], []
    for item in response.get("output", []):
        if item.get("type") == "web_search_call":
            action = item.get("action")
            calls.append({"id": item.get("id"), "status": item.get("status"), "action": action,
                          "sources": action.get("sources") if isinstance(action, dict) else None,
                          "note": "Отсутствующие поля API не восстановлены из текста модели."})
        if item.get("type") == "message":
            for part in item.get("content", []):
                citations.extend(annotation for annotation in part.get("annotations", []) if annotation.get("type") == "url_citation")
    return {"calls": calls, "citations": citations,
            "note": "open_page означает раскрытое действие, но не подтверждает успешное чтение текста страницы. Тело веб-ответа может не раскрываться API."}


class ResearchClient:
    def __init__(self, directory: Path, settings=None):
        key, configured = load_research_settings()
        self.settings = settings or configured
        self.key = key
        self.directory = directory
        self.requests = []
        self.client = OpenAI(api_key=key, base_url="https://api.openai.com/v1", max_retries=0,
                             timeout=self.settings.search_timeout_seconds)

    def close(self):
        self.client.close()

    def redact(self, value):
        """Не сохраняет ключ даже при его случайном повторении внутри ошибки API."""
        if isinstance(value, str):
            value = value.replace(self.key, "[КЛЮЧ СКРЫТ]")
            return re.sub(r"sk-[A-Za-z0-9_.-]{20,}|(?i:bearer)\s+\S+", "[КЛЮЧ СКРЫТ]", value)
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, dict):
            return {key: self.redact(item) for key, item in value.items() if key.casefold() != "authorization"}
        return value

    def save(self):
        atomic_write_json(self.directory / "research_usage.json", self.redact({"requests": self.requests}))

    def request(self, stage: str, round_number: int, schema, body: dict, timeout: int, max_retries: int = 0):
        record = {"number": len(self.requests) + 1, "stage": stage, "round": round_number,
                  "endpoint": "https://api.openai.com/v1/responses",
                  "started_at_utc": now_iso(), "ended_at_utc": None, "duration_seconds": None,
                  "status": "started", "request": body, "transport": {"timeout_seconds": timeout, "max_retries": max_retries},
                  "response_id": None, "model": None, "response": None, "usage": None, "web": None,
                  "error": None, "money_charged": None,
                  "unknown_fields_note": "Не полученные от API поля и фактическое денежное списание оставлены null."}
        self.requests.append(record)
        self.save()
        started = time.monotonic()
        try:
            meter = OpenAIMeter(self.directory, stage, self.key, record=record, persist=self.save)
            response = meter.create(self.client.with_options(timeout=timeout, max_retries=max_retries), **body)
            raw = response.model_dump(mode="json")
            record.update(response=raw, response_id=response.id, model=response.model,
                          usage=raw.get("usage"), web=web_metadata(raw), api_request_id=getattr(response, "_request_id", None))
            require_complete_response(response, result_name=stage)
            draft = schema.model_validate_json(response.output_text)
            if stage == "collection":
                calls = record["web"]["calls"]
                if not calls:
                    raise ResearchError("API не раскрыл ни одного вызова веб-инструмента; обычная генерация не заменяет сбор через веб.")
                if all(call.get("status") in {"failed", "incomplete"} for call in calls):
                    raise ResearchError("Все раскрытые обращения к веб-инструменту завершились сбоем.")
                if len(calls) > self.settings.max_tool_calls_per_round:
                    raise ResearchError("API вернул больше веб-вызовов, чем разрешено на раунд.")
            record["status"] = "completed"
            return draft, record
        except APIStatusError as error:
            details = self.redact(error.body)
            record.update(status="failed", error={"http_status": error.status_code, "body": details})
            raise ResearchError(f"OpenAI HTTP {error.status_code}: {json.dumps(details, ensure_ascii=False)}") from None
        except (APIConnectionError, APIError) as error:
            record.update(status="failed", error={"type": type(error).__name__, "message": "Нет соединения с OpenAI или ответ недоступен."})
            raise ResearchError(record["error"]["message"]) from None
        except (ValidationError, ResearchError) as error:
            message = str(error) if isinstance(error, ResearchError) else "Ответ не соответствует JSON Schema или правилам текущего этапа."
            record.update(status="failed", error={"type": type(error).__name__, "message": message})
            raise ResearchError(message) from None
        finally:
            record.update(ended_at_utc=now_iso(), duration_seconds=round(time.monotonic() - started, 3))
            self.save()

    def collect(self, topic: str, accumulated: dict, missing_questions: list[str], round_number: int):
        settings = self.settings
        body = {"model": settings.search_model, "reasoning": {"effort": settings.search_reasoning_effort},
                "tools": [{"type": "web_search", "search_context_size": settings.search_context_size}],
                "tool_choice": "required", "max_tool_calls": settings.max_tool_calls_per_round,
                "include": ["web_search_call.action.sources"], "store": False,
                "instructions": (PROMPT_DIRECTORY / "research_collection.txt").read_text() + FORMAT_INSTRUCTIONS,
                "input": json.dumps({"topic": topic, "accumulated": accumulated, "missing_questions": missing_questions}, ensure_ascii=False),
                "text": {"format": {"type": "json_schema", "name": "research_collection", "strict": True,
                                     "schema": CollectionResult.model_json_schema()}}}
        # Один Responses-запрос на раунд: общий бюджет веб-действий не умножается продолжениями.
        return self.request("collection", round_number, CollectionResult, body, settings.search_timeout_seconds)

    def assess(self, topic: str, accumulated: dict, round_number: int):
        settings = self.settings
        body = {"model": settings.assessment_model, "reasoning": {"effort": settings.assessment_reasoning_effort},
                "store": False,
                "instructions": (PROMPT_DIRECTORY / "research_sufficiency.txt").read_text() + FORMAT_INSTRUCTIONS,
                "input": json.dumps({"topic": topic, "accumulated_result": accumulated}, ensure_ascii=False),
                "text": {"format": {"type": "json_schema", "name": "research_sufficiency", "strict": True,
                                     "schema": SufficiencyResult.model_json_schema()}}}
        return self.request("assessment", round_number, SufficiencyResult, body, settings.assessment_timeout_seconds)
