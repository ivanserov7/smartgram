"""Только чтение административных Costs/Usage API; секреты и заголовки не сохраняются."""

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import httpx
from dotenv import dotenv_values

ENV_PATH = Path(__file__).with_name(".env")
API_ROOT = "https://api.openai.com/v1"


class BillingError(Exception):
    """Ошибка биллинга — не нулевые расходы и не ошибка производства ролика."""


def redact(value, secrets=()):
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[СКРЫТО]")
        value = re.sub(r"sk-[A-Za-z0-9_.-]{8,}|AIza[A-Za-z0-9_-]{20,}", "[СКРЫТО]", value)
        return re.sub(r"(?i)bearer\s+\S+|authorization[^\n]*", "[СКРЫТО]", value)
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: redact(item, secrets) for key, item in value.items()
                if not re.search(r"authorization|secret|redacted_value", key, re.I)}
    return value


def local_secret_values(values, include_environment=False):
    """ID ключа — публичный идентификатор, а не секретное значение ключа."""
    pairs = list(values.items()) + (list(os.environ.items()) if include_environment else [])
    return list(dict.fromkeys(value for name, value in pairs if value and not name.endswith(("_KEY_ID", "_KEY_IDS"))
            and re.search(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", name)))


@dataclass
class BillingSettings:
    admin_key: str = field(repr=False)
    generation_key: str = field(repr=False)
    project_id: str = ""
    api_key_ids: tuple = ()
    timeout_seconds: int = 20
    max_retries: int = 2


def load_billing_settings(env_path=None):
    values = {**dotenv_values(env_path or ENV_PATH, interpolate=False), **os.environ}
    def number(name, default, low, high):
        try:
            value = int(values.get(name) or default)
            if not low <= value <= high:
                raise ValueError
            return value
        except (TypeError, ValueError):
            raise BillingError(f"Недопустимая настройка {name}.") from None
    settings = BillingSettings(
        admin_key=(values.get("OPENAI_ADMIN_KEY") or "").strip(),
        generation_key=(values.get("OPENAI_API_KEY") or "").strip(),
        project_id=(values.get("OPENAI_BILLING_PROJECT_ID") or "").strip(),
        api_key_ids=tuple(item.strip() for item in (values.get("OPENAI_BILLING_API_KEY_IDS") or "").split(",") if item.strip()),
        timeout_seconds=number("OPENAI_BILLING_TIMEOUT_SECONDS", 20, 1, 120),
        max_retries=number("OPENAI_BILLING_MAX_RETRIES", 2, 0, 3))
    if settings.project_id and not re.fullmatch(r"proj_[A-Za-z0-9_-]+", settings.project_id):
        raise BillingError("OPENAI_BILLING_PROJECT_ID должен содержать ID проекта, не секрет.")
    if any(not re.fullmatch(r"key_[A-Za-z0-9_-]+", item) for item in settings.api_key_ids):
        raise BillingError("OPENAI_BILLING_API_KEY_IDS должен содержать ID вида key_..., не секреты.")
    return settings


class BillingClient:
    def __init__(self, settings, client=None, sleep=time.sleep):
        if not settings.admin_key:
            raise BillingError("Ожидаем данные: добавьте OPENAI_ADMIN_KEY в локальный .env.")
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.timeout_seconds, follow_redirects=False)
        self.sleep = sleep
        self.request_count = 0

    def close(self):
        self.client.close()

    def get(self, path, parameters):
        """Повторяет только временные ошибки чтения; 401/403 не повторяет."""
        if not path.startswith("/organization/") or ".." in path or "?" in path:
            raise BillingError("Недопустимый путь административного API.")
        secrets = (self.settings.admin_key, self.settings.generation_key)
        for attempt in range(self.settings.max_retries + 1):
            self.request_count += 1
            try:
                response = self.client.get(API_ROOT + path, params=parameters,
                    headers={"Authorization": "Bearer " + self.settings.admin_key}, timeout=self.settings.timeout_seconds)
            except httpx.HTTPError:
                if attempt < self.settings.max_retries:
                    self.sleep(min(2 ** attempt, 4))
                    continue
                raise BillingError("Ошибка сети или тайм-аут чтения OpenAI Billing API; расходы неизвестны.") from None
            if response.status_code in {429, 500, 502, 503, 504} and attempt < self.settings.max_retries:
                self.sleep(min(2 ** attempt, 4))
                continue
            if response.status_code != 200:
                try:
                    details = redact(response.json().get("error", {}), secrets)
                except (ValueError, AttributeError):
                    details = {"message": "Ответ не содержит JSON ошибки."}
                reason = "Неверный админский ключ" if response.status_code == 401 else "Недостаточно административных прав" if response.status_code == 403 else "Ошибка API"
                raise BillingError(f"{reason}: HTTP {response.status_code}: {json.dumps(details, ensure_ascii=False)}")
            try:
                # Деньги читаются Decimal сразу из исходных байтов, без промежуточного float.
                data = json.loads(response.text, parse_float=Decimal)
                if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                    raise ValueError
                # Исходное тело (без HTTP-заголовков) остаётся локально вместе с разобранными Decimal.
                data["raw_response_text"] = redact(response.text, secrets)
                return data
            except (ValueError, TypeError):
                raise BillingError("Billing API вернул повреждённый JSON или неизвестную структуру.") from None

    def pages(self, path, parameters, cursor="page"):
        pages, seen = [], set()
        params = list(parameters)
        while True:
            data = self.get(path, params)
            pages.append(data)
            if not data.get("has_more"):
                return pages
            next_page = data.get("next_page") if cursor == "page" else data.get("last_id")
            if not next_page and cursor == "after" and data["data"]:
                next_page = data["data"][-1].get("id")
            if not isinstance(next_page, str) or not next_page or next_page in seen:
                raise BillingError("Некорректная пагинация Billing API; неполные данные не приняты.")
            seen.add(next_page)
            if len(pages) >= 1000:
                raise BillingError("Превышен защитный предел страниц Billing API.")
            params = [(key, value) for key, value in params if key != cursor] + [(cursor, next_page)]


def key_matches(secret, masked):
    """Маска служит только локальному сопоставлению; она не попадает в экспорт."""
    if not secret or not isinstance(masked, str):
        return False
    parts = re.split(r"\.{3,}|…+|\*+", masked)
    return len(parts) == 2 and len(parts[0]) >= 3 and len(parts[1]) >= 3 and secret.startswith(parts[0]) and secret.endswith(parts[1])


def discover_scope(api: BillingClient):
    """Не приписывает весь проект Smartgram. По умолчанию выбирает только однозначно найденный рабочий ключ."""
    settings = api.settings
    if settings.project_id and settings.api_key_ids:
        return {"project_id": settings.project_id, "api_key_ids": list(settings.api_key_ids),
                "key_fingerprints": {}, "selection": "explicit_configuration"}
    projects = [{"id": settings.project_id}] if settings.project_id else [
        item for page in api.pages("/organization/projects", [("limit", 100), ("include_archived", "true")], "after") for item in page["data"]]
    matches = []
    for project in projects:
        project_id = project.get("id")
        if not isinstance(project_id, str) or not re.fullmatch(r"proj_[A-Za-z0-9_-]+", project_id):
            raise BillingError("Административный API вернул некорректный ID проекта.")
        for page in api.pages(f"/organization/projects/{project_id}/api_keys", [("limit", 100)], "after"):
            for key in page["data"]:
                if key_matches(settings.generation_key, key.get("redacted_value")):
                    matches.append((project_id, key["id"]))
    matches = list(dict.fromkeys(matches))
    if len(matches) != 1:
        raise BillingError("Не удалось однозначно определить рабочий ключ Smartgram. Укажите OPENAI_BILLING_PROJECT_ID и OPENAI_BILLING_API_KEY_IDS (ID, не секреты). Расходы организации не присвоены проекту.")
    project_id, key_id = matches[0]
    return {"project_id": project_id, "api_key_ids": [key_id], "selection": "unique_redacted_key_match",
            "key_fingerprints": {hashlib.sha256(settings.generation_key.encode()).hexdigest(): key_id}}


def usage_parameters(start, end, scope, groups):
    return [("start_time", start), ("end_time", end), ("bucket_width", "1d"), ("limit", 30),
            ("project_ids", scope["project_id"])] + [("api_key_ids", key) for key in scope["api_key_ids"]] + [("group_by", group) for group in groups]


def money_text(value):
    """Читаемая точная десятичная строка, без научной записи и лишних конечных нулей."""
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text
