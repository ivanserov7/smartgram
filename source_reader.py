"""Загрузка доступного текста источника. Сниппет поиска не заменяет страницу."""

import ipaddress
import socket
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx


MAX_BYTES = 2_000_000
MAX_TEXT_CHARACTERS = 24_000


def page_problem(text: str) -> str:
    """HTTP 200 не гарантирует статью: отсекаем короткие ответы и страницы-заглушки."""
    normalized = " ".join(text.casefold().split())
    markers = ("verify you are human", "checking your browser", "just a moment", "access denied",
               "enable javascript and cookies", "please complete the captcha", "доступ запрещен", "подтвердите, что вы человек")
    if any(marker in normalized for marker in markers):
        return "Вместо материала получена заглушка, капча или сообщение об ограничении доступа."
    if len(normalized) < 80:
        return "Недостаточно содержательного текста: возможна заглушка или требование JavaScript."
    return ""


class PageTextParser(HTMLParser):
    """Убирает разметку и содержимое script/style из HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.hidden_depth += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data):
        if not self.hidden_depth:
            self.parts.append(data)


def validate_public_url(url: str) -> None:
    """Не разрешает читать локальные файлы и адреса внутренней сети."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Недопустимый адрес источника.")
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("Источник не является публичной веб-страницей.")


def download_text(url: str) -> tuple[str, str, bool]:
    """Скачивает HTML/plain text с ограничением размера и проверкой редиректов."""
    current_url = url
    with httpx.Client(timeout=20.0, follow_redirects=False, trust_env=False,
                      headers={"User-Agent": "Smartgram/0.1 (source verification)"}) as client:
        for _ in range(6):
            validate_public_url(current_url)
            with client.stream("GET", current_url) as response:
                if response.is_redirect:
                    current_url = urljoin(current_url, response.headers["location"])
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";")[0].lower()
                if content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
                    raise ValueError("Формат не поддержан: нужны HTML или простой текст; PDF пока не читается.")
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_BYTES:
                        raise ValueError("Источник превышает ограничение размера загрузки.")
                text = content.decode(response.encoding or "utf-8", errors="replace")
                if content_type != "text/plain":
                    parser = PageTextParser()
                    parser.feed(text)
                    text = " ".join(parser.parts)
                text = " ".join(text.split())
                problem = page_problem(text)
                if problem:
                    raise ValueError(problem)
                return text[:MAX_TEXT_CHARACTERS], current_url, len(text) > MAX_TEXT_CHARACTERS
    raise ValueError("Слишком много перенаправлений источника.")


def fetch_source(url: str) -> dict:
    """Недоступность сохраняется как результат, без ложного подтверждения."""
    result = {"url": url, "fetched_at": datetime.now(timezone.utc).isoformat()}
    try:
        text, final_url, truncated = download_text(url)
        return {**result, "status": "available", "text": text, "final_url": final_url,
                "truncated": truncated, "reason": ""}
    except httpx.HTTPStatusError as error:
        reason = f"Источник недоступен: HTTP {error.response.status_code}."
    except (httpx.HTTPError, httpx.InvalidURL, OSError):
        reason = "Не удалось загрузить источник: ошибка сети или таймаут."
    except ValueError as error:
        reason = str(error)
    return {**result, "status": "unavailable", "text": "", "reason": reason}


def fetch_research_sources(research: dict, fetch=fetch_source) -> list[dict]:
    """Загружает один снимок каждой страницы; его используют все попытки проверки."""
    urls = dict.fromkeys(source["url"] for fact in research["facts"] for source in fact["sources"])
    return [fetch(url) for url in urls]
