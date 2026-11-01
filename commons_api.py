"""Бесплатный Wikimedia Commons Action API: поиск, метаданные и загрузка файлов."""

import json
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values


API_URL = "https://commons.wikimedia.org/w/api.php"
CANDIDATE_LIMIT = 5
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_API_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT = 15.0
DOWNLOAD_DEADLINE = 30.0
USER_AGENT = "SmartgramBot/0.1 (local educational-video prototype; interactive use)"


class VisualError(Exception):
    """Понятная ошибка подбора изображений."""


class VisualServiceError(VisualError):
    """Сбой сервиса/сети: новый поисковый запрос его не исправит."""


class MetadataTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(value) -> str:
    """Метаданные Commons содержат HTML; в манифесте сохраняем обычный текст."""
    if not isinstance(value, str):
        return ""
    parser = MetadataTextParser()
    parser.feed(value)
    return " ".join(" ".join(parser.parts).split())


def metadata_value(metadata: dict, name: str) -> str:
    field = metadata.get(name, {})
    return plain_text(field.get("value")) if isinstance(field, dict) else ""


def accepted_license(name: str, url: str) -> str | None:
    """Узкий список распознаваемых лицензий; неизвестные и несовпадающие пропускаем."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or parsed.netloc not in {"creativecommons.org", "www.creativecommons.org"}:
        return None
    normalized_name = re.sub(r"[^a-z0-9]", "", name.casefold())
    # Локализованная страница deed описывает ту же лицензию. Исходный URL сохраняется.
    license_path = re.sub(r"/deed\.[a-zA-Z_-]+/?$", "", parsed.path)
    match = re.fullmatch(r"/licenses/(by|by-sa)/(1\.0|2\.0|2\.5|3\.0|4\.0)(?:/[a-z]{2})?/?", license_path)
    if match:
        kind, version = match.groups()
        expected = re.sub(r"[^a-z0-9]", "", f"CC {kind} {version}".casefold())
        if normalized_name == expected:
            return f"cc-{kind}-{version}"
    path = license_path.rstrip("/")
    if path == "/publicdomain/zero/1.0" and normalized_name in {"cc0", "cc010", "cc010universal"}:
        return "cc0-1.0"
    if path == "/publicdomain/mark/1.0" and normalized_name in {"publicdomain", "publicdomainmark10", "pdm10"}:
        return "public-domain-mark-1.0"
    return None


def parse_candidate(page: dict) -> dict:
    """Оставляет данные API и условия использования, не подменяя автора загрузчиком."""
    info = page["imageinfo"][0]
    metadata = info.get("extmetadata", {})
    license_name = metadata_value(metadata, "LicenseShortName")
    license_url = metadata_value(metadata, "LicenseUrl")
    has_thumbnail = bool(info.get("thumburl"))
    return {
        "file_title": page["title"], "title": metadata_value(metadata, "ObjectName") or page["title"],
        "description": metadata_value(metadata, "ImageDescription"),
        "author": metadata_value(metadata, "Artist"), "credit": metadata_value(metadata, "Credit"),
        "source_page": info.get("descriptionurl", ""), "original_url": info.get("url", ""),
        "download_url": info.get("thumburl") or info.get("url", ""),
        "mime": info.get("thumbmime", info.get("mime")) if has_thumbnail else info.get("mime"),
        "original_mime": info.get("mime"), "width": info.get("thumbwidth" if has_thumbnail else "width", 0),
        "height": info.get("thumbheight" if has_thumbnail else "height", 0),
        "license": license_name, "license_url": license_url,
        "license_id": accepted_license(license_name, license_url),
        "usage_terms": metadata_value(metadata, "UsageTerms"),
        "attribution_required": metadata_value(metadata, "AttributionRequired"),
        "restrictions": metadata_value(metadata, "Restrictions"),
        "requires_license_review": True,
    }


def create_commons_client() -> httpx.Client:
    """Ключи и платные API здесь не используются; автоматических повторов нет."""
    values = dotenv_values(Path(__file__).with_name(".env"), interpolate=False)
    user_agent = values.get("WIKIMEDIA_USER_AGENT", USER_AGENT)
    if (not isinstance(user_agent, str) or not user_agent.strip() or not user_agent.isascii()
            or "\n" in user_agent or "\r" in user_agent):
        raise VisualError("WIKIMEDIA_USER_AGENT должен быть непустой строкой ASCII без переносов.")
    return httpx.Client(headers={"User-Agent": user_agent}, timeout=REQUEST_TIMEOUT, follow_redirects=False)


def bounded_get(client: httpx.Client, url: str, limit: int, params=None, response_headers=None) -> tuple[bytes, str]:
    """Ограничивает и объём, и время потоковой загрузки ответа."""
    started = time.monotonic()
    with client.stream("GET", url, params=params) as response:
        if response_headers is not None:
            response_headers["retry-after"] = response.headers.get("retry-after")
        response.raise_for_status()
        declared_size = response.headers.get("content-length")
        if declared_size and int(declared_size) > limit:
            raise VisualError("Ответ превышает допустимый размер загрузки.")
        content = bytearray()
        for chunk in response.iter_bytes():
            if time.monotonic() - started > DOWNLOAD_DEADLINE:
                raise VisualServiceError("Превышено время загрузки Wikimedia.")
            content.extend(chunk)
            if len(content) > limit:
                raise VisualError("Ответ превышает допустимый размер загрузки.")
        return bytes(content), response.headers.get("content-type", "").split(";")[0].lower()


def search_images(query: str, client: httpx.Client) -> list[dict]:
    """Один поиск до пяти файлов с лицензией, описанием и размером превью."""
    params = {
        "action": "query", "format": "json", "formatversion": 2, "generator": "search",
        "gsrsearch": query, "gsrnamespace": 6, "gsrlimit": CANDIDATE_LIMIT,
        "prop": "imageinfo", "iiprop": "url|size|mime|thumbmime|extmetadata", "iiurlwidth": 2560,
        "iiextmetadatalanguage": "en", "maxlag": 5,
    }
    try:
        content, _ = bounded_get(client, API_URL, MAX_API_BYTES, params)
        response = json.loads(content)
        if isinstance(response, dict) and "error" in response:
            details = response["error"]
            raise VisualServiceError("Wikimedia API: " + json.dumps(details, ensure_ascii=False))
        if not isinstance(response, dict):
            raise VisualServiceError("Wikimedia API вернул некорректные данные.")
        pages = response.get("query", {}).get("pages", [])
        if not isinstance(pages, list):
            raise ValueError
        candidates = []
        for page in pages[:CANDIDATE_LIMIT]:
            try:
                candidates.append(parse_candidate(page))
            except (KeyError, TypeError, IndexError, AttributeError):
                continue
        return candidates
    except httpx.HTTPStatusError as error:
        raise VisualServiceError(f"Wikimedia API недоступен: HTTP {error.response.status_code}.") from None
    except httpx.HTTPError as error:
        raise VisualServiceError(f"Wikimedia API: {type(error).__name__}: ошибка сети или таймаут.") from None
    except (ValueError, TypeError, AttributeError):
        raise VisualServiceError("Wikimedia API вернул некорректные данные.") from None


def download_image(url: str, client: httpx.Client) -> bytes:
    """Не скачивает произвольные URL из метаданных; только сервер изображений Commons."""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc not in {"upload.wikimedia.org", "thumb.wikimedia.org"}
                or not parsed.path.startswith("/wikipedia/commons/") or parsed.username or parsed.password):
            raise VisualError("Недопустимый адрес изображения Wikimedia Commons.")
        content, mime = bounded_get(client, url, MAX_IMAGE_BYTES)
        if mime not in {"image/jpeg", "image/png"}:
            raise VisualError("Сервер вернул не JPEG/PNG.")
        return content
    except httpx.HTTPStatusError as error:
        raise VisualServiceError(f"Изображение не скачивается: HTTP {error.response.status_code}.") from None
    except httpx.HTTPError as error:
        raise VisualServiceError(f"Изображение: {type(error).__name__}: ошибка сети или таймаут.") from None
    except ValueError:
        raise VisualError("Некорректный URL или размер ответа изображения.") from None
