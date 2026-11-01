"""Клиент сценариста использует существующее журналирование Responses API."""

import json

from openai import OpenAI

from fact_checker import VerificationDraft, validate_verification
from file_io import atomic_write_json
from research import ResearchError
from research_requests import FORMAT_INSTRUCTIONS, ResearchClient
from script_config import load_script_settings


class ScriptClient(ResearchClient):
    def __init__(self, directory, settings=None):
        key, configured = load_script_settings()
        self.key = key
        self.settings = settings or configured
        self.directory = directory
        path = directory / "script_usage.json"
        try:
            self.requests = json.loads(path.read_text())["requests"] if path.exists() else []
            if not isinstance(self.requests, list):
                raise ValueError
            if self.requests and (self.requests[-1].get("status") != "completed" or self.requests[-1].get("stage") == "script_writing"):
                raise ResearchError("Предыдущее API-обращение или проверка не завершены. Автоматический повтор отключён; журнал сохранён.")
        except (OSError, ValueError, KeyError, TypeError):
            raise ResearchError("Повреждён script_usage.json; история запросов не заменена. Продолжение остановлено.") from None
        self.client = OpenAI(api_key=key, base_url="https://api.openai.com/v1", max_retries=0,
                             timeout=self.settings.writing_timeout_seconds)

    def save(self):
        atomic_write_json(self.directory / "script_usage.json", self.redact({"requests": self.requests}))

    def generate(self, schema, instructions, data):
        verification = schema is VerificationDraft
        step = "verification" if verification else "writing"
        settings = self.settings
        body = {"model": getattr(settings, step + "_model"),
                "reasoning": {"effort": getattr(settings, step + "_reasoning_effort")},
                "store": False, "instructions": instructions + FORMAT_INSTRUCTIONS,
                "input": json.dumps(data, ensure_ascii=False),
                "text": {"format": {"type": "json_schema", "name": "script_" + step, "strict": True,
                                     "schema": schema.model_json_schema()}}}
        if verification:
            body.update(tools=[{"type": "web_search", "search_context_size": settings.verification_context_size}],
                        tool_choice="required", max_tool_calls=settings.verification_max_tool_calls,
                        include=["web_search_call.action.sources"])
        draft, record = self.request("script_" + step, data.get("version", len(self.requests) // 2 + 1), schema, body,
                                     getattr(settings, step + "_timeout_seconds"), getattr(settings, step + "_max_retries"))
        if verification:
            calls = record["web"]["calls"]
            if not calls or not any(call.get("status") == "completed" for call in calls):
                message = "Проверяющий не выполнил успешного раскрытого веб-обращения. Одобрение не принимается."
            elif len(calls) > settings.verification_max_tool_calls:
                message = "Проверяющий превысил SCRIPT_VERIFICATION_MAX_TOOL_CALLS."
            else:
                message = None
            if message:
                record.update(status="failed", error={"type": "WebVerificationError", "message": message})
                self.save()
                raise ResearchError(message)
            try:
                validate_verification(draft.model_dump(), {"fragments": data["fragments"]})
            except ResearchError as error:
                record.update(status="failed", error={"type": "VerificationFormatError", "message": str(error)})
                self.save()
                raise
        return draft
