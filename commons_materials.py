"""Поиск, проверяемые права и производные файлы через официальный Commons API."""

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
import hashlib
import json

from commons_api import API_URL, parse_candidate, accepted_license
from file_io import atomic_write_json
from image_files import inspect_image
from production_state import ProductionError, digest, inside, sha


def image_parameters(side=3072):
    return {
        "prop": "imageinfo|categories",
        "iiprop": "url|size|mime|thumbmime|extmetadata|sha1|timestamp",
        "iiurlwidth": side,
        "iiurlheight": side,
        "iiextmetadatalanguage": "en",
        "cllimit": 10,
    }


def parse_pages(response):
    result = []
    for page in sorted(response.get("query", {}).get("pages", []), key=lambda p: p.get("index", 10**9)):
        try:
            item = parse_candidate(page)
            info = page["imageinfo"][0]
            item.update(
                search_rank=page.get("index"),
                page_id=page.get("pageid"),
                original_width=info.get("width"),
                original_height=info.get("height"),
                original_size_bytes=info.get("size"),
                source_sha1=info.get("sha1"),
                source_timestamp=info.get("timestamp"),
                thumbnail_url=info.get("thumburl"),
                thumbnail_mime=info.get("thumbmime"),
                raw_extmetadata=info.get("extmetadata", {}),
                categories=[
                    c["title"]
                    for c in page.get("categories", [])
                    if c.get("title", "").startswith("Category:")
                ],
            )
            result.append(item)
        except (KeyError, TypeError, IndexError):
            continue
    return result


def search_files(gateway, query, continuation=None, *, limit=10, side=3072):
    params = {
        "action": "query",
        "format": "json",
        "formatversion": 2,
        "generator": "search",
        "gsrnamespace": 6,
        "gsrsearch": query,
        "gsrlimit": limit,
        "maxlag": 5,
        **image_parameters(side),
        **(continuation or {}),
    }
    response = gateway.get(params)
    return {
        "candidates": parse_pages(response),
        "continue": response.get("continue", {}),
        "params": params,
    }


def search_categories(gateway, query):
    response = gateway.get(
        {
            "action": "query",
            "format": "json",
            "formatversion": 2,
            "list": "search",
            "srnamespace": 14,
            "srsearch": query,
            "srlimit": 3,
            "maxlag": 5,
        }
    )
    return [
        p["title"]
        for p in response.get("query", {}).get("search", [])
        if p.get("title", "").startswith("Category:")
    ]


def category_files(gateway, category):
    # category попадает сюда исключительно из сохранённого ответа API, а не от модели.
    params = {
        "action": "query",
        "format": "json",
        "formatversion": 2,
        "generator": "categorymembers",
        "gcmtitle": category,
        "gcmtype": "file",
        "gcmnamespace": 6,
        "gcmlimit": 10,
        "maxlag": 5,
        **image_parameters(),
    }
    response = gateway.get(params)
    return {
        "candidates": parse_pages(response),
        "continue": response.get("continue", {}),
        "params": params,
    }


class LicenseLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []

    def handle_starttag(self, tag, attrs):
        href = dict(attrs).get("href", "")
        if tag == "a" and href:
            self.links.append("https:" + href if href.startswith("//") else href)


def public_domain_proof(candidate):
    proof = candidate.get("rights_evidence", {})
    return (
        proof.get("status") == "public_domain"
        and proof.get("source_page") == candidate.get("source_page")
        and bool(proof.get("templates"))
        and bool(proof.get("snapshot_sha256"))
    )


def resolve_rights(candidate, gateway):
    """Без LicenseUrl читаем страницу/шаблоны. Имя лицензии само по себе не доказательство."""
    if candidate.get("origin")=="pexels":
        from pexels_materials import verify_source
        verify_source(candidate,gateway.journal.directory)
        return candidate
    if accepted_license(candidate.get("license", ""), candidate.get("license_url", "")):
        return candidate
    response = gateway.get(
        {
            "action": "parse",
            "format": "json",
            "formatversion": 2,
            "page": candidate["file_title"],
            "prop": "text|templates",
            "maxlag": 5,
        },
        kind="metadata",
    )
    parsed = response.get("parse", {})
    if parsed.get("title") != candidate["file_title"]:
        raise ProductionError(
            "Не подтверждена страница исходного файла для проверки прав."
        )
    snapshot = (
        gateway.journal.directory
        / "visual_materials"
        / "rights"
        / (digest(response)[:20] + ".json")
    )
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(snapshot, response)
    evidence = {
        "source_page": candidate["source_page"],
        "snapshot_path": str(snapshot.relative_to(gateway.journal.directory)),
        "snapshot_sha256": sha(snapshot),
        "method": "Commons action=parse text/templates",
    }
    links = LicenseLinks()
    links.feed(parsed.get("text", ""))
    for url in links.links:
        if accepted_license(candidate.get("license", ""), url):
            return dict(
                candidate,
                license_url=url,
                rights_evidence=dict(evidence, status="licensed"),
            )
    templates = {t.get("title") or t.get("*") for t in parsed.get("templates", [])}
    # Эти шаблоны содержат конкретное основание PD. PD-Art/PD-US alone недостаточно.
    approved = {
        "Template:PD-self",
        "Template:PD-user",
        "Template:PD-author",
        "Template:PD-old-100-expired",
        "Template:PD-USGov-NASA",
        "Template:PD-USGov",
        "Template:PD-textlogo",
        "Template:PD-ineligible",
    }
    known = sorted(templates & approved)
    metadata = candidate.get("raw_extmetadata", {})
    copyrighted = str(metadata.get("Copyrighted", {}).get("value", "")).casefold()
    license_name = candidate.get("license", "").casefold()
    if (
        known
        and copyrighted in {"false", "no", "0"}
        and ("public domain" in license_name or license_name.startswith("pd"))
    ):
        return dict(
            candidate,
            license="Public domain",
            license_url="",
            rights_evidence=dict(
                evidence,
                status="public_domain",
                templates=known,
                basis="Commons сообщает Copyrighted=false; основание указано в лицензионных шаблонах страницы.",
            ),
        )
    raise ProductionError(
        "Правовой статус не подтверждён страницей Commons; найденные шаблоны/ссылки не дают пригодного разрешения."
    )


def basic_problem(candidate):
    if candidate.get("origin")=="pexels":
        from pexels_materials import metadata_problem
        return metadata_problem(candidate)
    mime = candidate.get("original_mime")
    if mime not in {"image/jpeg", "image/png", "image/svg+xml"}:
        return "Неподдерживаемый тип исходного произведения: " + str(mime)
    if not candidate.get("source_page") or not candidate.get("file_title"):
        return "Нет страницы исходного произведения."
    if candidate.get("restrictions"):
        return "Неразрешённые ограничения: " + candidate["restrictions"]
    if (
        mime != "image/svg+xml"
        and max(
            candidate.get("original_width") or candidate.get("width", 0),
            candidate.get("original_height") or candidate.get("height", 0),
        )
        < 1080
    ):
        return "Недостаточное разрешение исходной фотографии."
    return None


def fetch_material(candidate, gateway, *, download_kind="download"):
    """SVG не исполняется локально: храним его байты, используем PNG от Wikimedia."""
    cid = candidate["candidate_id"]
    folder = gateway.journal.directory / "visual_materials" / "originals"
    folder.mkdir(parents=True, exist_ok=True)
    svg = candidate["original_mime"] == "image/svg+xml"
    source_path = None
    if svg:
        source_path = folder / (cid + ".svg")
        original = gateway.get(
            {},
            kind=download_kind,
            url=safe_image_url(candidate["original_url"]),
            destination=source_path,
        )
        if original["mime"] not in {"image/svg+xml", "text/xml", "application/xml"}:
            raise ProductionError("Сервер вернул неверный MIME исходного SVG.")
        if (
            not candidate.get("thumbnail_url")
            or candidate.get("thumbnail_mime") != "image/png"
        ):
            raise ProductionError(
                "API не предоставил PNG-растр SVG; небезопасная локальная растеризация не используется."
            )
    # Предпочитаем предоставленный API пригодный растр; тяжёлый оригинал не скачиваем.
    url = candidate.get("thumbnail_url") or candidate["original_url"]
    expected_mime = (
        candidate.get("thumbnail_mime")
        if candidate.get("thumbnail_url")
        else candidate["original_mime"]
    )
    if expected_mime not in {"image/png", "image/jpeg"}:
        raise ProductionError("API не предоставил поддерживаемую растровую версию.")
    target = folder / (cid + (".png" if expected_mime == "image/png" else ".jpg"))
    record = gateway.get(
        {}, kind=download_kind, url=safe_image_url(url), destination=target
    )
    if record["mime"] != expected_mime:
        raise ProductionError(
            "Фактический MIME загрузки не совпадает с предоставленной версией."
        )
    measured = inspect_image(target.read_bytes(), minimum_width=1, minimum_height=1)
    expected_format = "PNG" if expected_mime == "image/png" else "JPEG"
    if measured["format"] != expected_format:
        raise ProductionError("Формат декодированного растра не соответствует MIME.")
    if min(1080 / measured["width"], 1920 / measured["height"]) > 1:
        raise ProductionError(
            "Растр недостаточен даже для показа целиком без увеличения."
        )
    return dict(
        candidate,
        **measured,
        original_path=str(target.relative_to(gateway.journal.directory)),
        used_url=url,
        used_mime=expected_mime,
        derivative_of=candidate["original_url"],
        source_original_path=(
            str(source_path.relative_to(gateway.journal.directory))
            if source_path
            else None
        ),
        source_original_sha256=sha(source_path) if source_path else None
    )


def safe_image_url(url):
    value = urlsplit(url)
    if (
        value.scheme != "https"
        or value.netloc not in {"upload.wikimedia.org", "thumb.wikimedia.org"}
        or not value.path.startswith("/wikipedia/commons/")
        or value.username
        or value.password
    ):
        raise ProductionError("Неподтверждённый адрес версии Wikimedia.")
    return url
