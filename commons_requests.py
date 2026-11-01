"""Ограниченные повторы только безопасных Commons GET. Платный Journal не меняется."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import time

import httpx

from commons_api import (
    API_URL,
    MAX_API_BYTES,
    bounded_get,
    VisualError,
    VisualServiceError,
)
from file_io import atomic_write
from production_state import ProductionError, ProductionServiceError, digest, sha


class CommonsPaused(ProductionServiceError):
    def __init__(self, message, retry_at=None, temporary=True):
        super().__init__(message)
        self.retry_at = retry_at
        self.temporary = temporary


class RequestExhausted(ProductionError):
    """После паузы можно выбрать иной запрос, но не сбросить потраченные попытки."""


def retry_delay(value, fallback, now):
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - now
        except (TypeError, ValueError, OverflowError):
            delay = fallback
    return max(fallback, delay)


class CommonsRequests:
    def __init__(
        self,
        journal,
        budget,
        job_key,
        client,
        settings,
        sleep=time.sleep,
        now=time.time,
    ):
        self.journal, self.budget, self.job_key = journal, budget, job_key
        self.client, self.settings, self.sleep, self.now = client, settings, sleep, now

    def get(self, params, kind="search", url=API_URL, destination=None):
        """Один логический запрос; его HTTP-попытки и бюджет пишутся до сети."""
        identity = {
            "job": self.job_key,
            "round": self.budget["number"],
            "kind": kind,
            "url": url,
            "params": params,
        }
        key = digest({"step": "commons_get", "identity": identity})
        entry = self.journal.state["operations"].get(key)
        counter, limit = {
            "search": (
                "calls",
                self.settings["max_search_api_calls_per_fragment_per_round"],
            ),
            "metadata": (
                "metadata_calls",
                self.settings.get("max_metadata_calls_per_round", 6),
            ),
            "download": ("download_calls", self.settings.get("max_download_calls_per_round", 10)),
            "preview": ("preview_calls", self.settings.get("preview_limit", 20)),
            "finalist": ("finalist_calls", self.settings.get("finalist_limit", 3) * 2),
        }[kind]
        if entry and entry["status"] == "completed":
            result = entry["result"]
            if (
                destination
                and sha(self.journal.directory / result["path"]) != result["sha256"]
            ):
                raise ProductionError("Сохранённая загрузка изменена; кеш не принят.")
            return result
        if entry is None:
            if self.budget.get(counter, 0) >= limit:
                raise RequestExhausted(
                    f"Исчерпан лимит {kind}: {limit} новых обращений на раунд."
                )
            self.budget[counter] = self.budget.get(counter, 0) + 1
            entry = {
                "step": "commons_get",
                "identity": identity,
                "status": "started",
                "attempts": [],
                "result": None,
            }
            self.journal.state["operations"][key] = entry
            self.journal.save()
        elif entry.get("local_file_error"):
            raise ProductionError(entry["reason"])
        elif entry.get("permanent"):
            raise CommonsPaused(entry["reason"], temporary=False)
        if entry.get("retry_at", 0) > self.now():
            raise CommonsPaused(entry["reason"], entry["retry_at"])
        maximum_retries = self.settings.get("max_network_retries_per_round", 2)
        while True:
            number = len(entry["attempts"])
            if number >= 3 or (
                number and self.budget.get("retry_calls", 0) >= maximum_retries
            ):
                entry["status"] = "exhausted"
                self.journal.save()
                raise RequestExhausted(
                    "HTTP-попытки этого GET исчерпаны; успешные ответы сохранены."
                )
            if number:
                self.budget["retry_calls"] = self.budget.get("retry_calls", 0) + 1
            attempt = {
                "number": number + 1,
                "started_at": self.now(),
                "status": "started",
            }
            entry["attempts"].append(attempt)
            self.journal.save()
            transient, delay = False, 2 ** (number + 1)
            try:
                self.sleep(self.settings.get("request_interval_seconds", 0.3))
                limit_bytes = MAX_API_BYTES if destination is None else 10 * 1024 * 1024
                headers = {}
                content, mime = bounded_get(
                    self.client,
                    url,
                    limit_bytes,
                    params or None,
                    response_headers=headers,
                )
                if destination is None:
                    result = json.loads(content)
                    if not isinstance(result, dict):
                        raise VisualError("Некорректный JSON Commons.")
                    if result.get("error"):
                        error = result["error"]
                        transient = error.get("code") in {
                            "maxlag",
                            "cirrussearch-too-busy-error",
                            "ratelimited",
                            "readonly",
                        }
                        raise VisualError(
                            "Commons API: " + json.dumps(error, ensure_ascii=False)
                        )
                else:
                    atomic_write(destination, content)
                    result = {
                        "path": str(destination.relative_to(self.journal.directory)),
                        "sha256": sha(destination),
                        "mime": mime,
                        "size_bytes": len(content),
                    }
                attempt.update(status="completed", ended_at=self.now())
                entry.update(status="completed", result=result, retry_at=None)
                self.journal.save()
                return result
            except httpx.HTTPStatusError as error:
                status = error.response.status_code
                transient = status == 429 or 500 <= status <= 599
                delay = retry_delay(
                    error.response.headers.get("Retry-After"), delay, self.now()
                )
                message = f"Wikimedia HTTP {status}"
                attempt["http_status"] = status
                if destination is not None and status in {400, 404, 410, 422}:
                    attempt.update(status="failed", reason=message, ended_at=self.now())
                    entry.update(status="failed", reason=message, local_file_error=True)
                    self.journal.save()
                    raise ProductionError(
                        message
                        + "; недоступен конкретный файл, остальные кандидаты допустимы."
                    ) from None
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                transient = True
                message = f"Wikimedia {type(error).__name__}: временная ошибка сети."
            except httpx.HTTPError as error:
                message = f"Wikimedia {type(error).__name__}: запрос не выполнен."
            except (VisualError, ValueError) as error:
                message = str(error)
                transient = transient or isinstance(error, VisualServiceError)
                if transient:
                    delay = retry_delay(headers.get("retry-after"), delay, self.now())
                # Превышение размера — локальный отказ файла, не перегрузка сервиса.
                if destination is not None and not transient:
                    attempt.update(status="failed", reason=message, ended_at=self.now())
                    entry.update(status="failed", reason=message)
                    self.journal.save()
                    raise ProductionError(message) from None
            attempt.update(
                status="failed",
                reason=message,
                ended_at=self.now(),
                backoff_seconds=delay if transient else None,
            )
            entry.update(
                status="paused" if transient else "failed",
                reason=message,
                retry_at=self.now() + max(delay, 60) if transient else None,
                permanent=not transient,
            )
            self.journal.save()
            if not transient:
                raise CommonsPaused(message, temporary=False) from None
            can_retry = (
                len(entry["attempts"]) < 3
                and self.budget.get("retry_calls", 0) < maximum_retries
            )
            if not can_retry or delay > 30:
                raise CommonsPaused(message, entry["retry_at"]) from None
            attempt["slept_seconds"] = delay
            self.journal.save()
            self.sleep(delay)
