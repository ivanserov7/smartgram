"""Небольшой адаптер фотографий Pexels; тот же HTTP-клиент и журнал визуального этапа."""

from datetime import datetime, timezone
import json
import time
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values

from file_io import atomic_write_json
from image_files import inspect_image
from production_state import ROOT, ProductionError, digest, inside, read, sha

API_URL = "https://api.pexels.com/v1/search"
LICENSE_URL = "https://www.pexels.com/license/"
TERMS_URL = "https://www.pexels.com/terms-of-service/"
TIMEOUT = 8.0


def image_url(url, photo_id):
    """Ключ никогда не отправляется CDN; принимаем только фотографию из ответа API."""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "images.pexels.com"
        or parsed.username
        or parsed.password
        or not parsed.path.startswith(f"/photos/{photo_id}/")
        or parsed.fragment
    ):
        raise ProductionError("Неподтверждённый адрес фотографии Pexels.")
    return url


def metadata_problem(candidate):
    if any(
        not isinstance(candidate.get(k), str) or not candidate[k].strip()
        for k in (
            "source_page",
            "photographer_url",
            "original_url",
            "thumbnail_url",
            "author",
        )
    ):
        return "Неполные данные происхождения Pexels."
    photo_id = candidate.get("pexels_id")
    if type(photo_id) is not int or photo_id <= 0:
        return "Нет действительного ID Pexels."
    page = urlsplit(candidate.get("source_page", ""))
    author = urlsplit(candidate.get("photographer_url", ""))
    if (
        page.scheme != "https"
        or page.netloc != "www.pexels.com"
        or not page.path.startswith("/photo/")
        or not page.path.rstrip("/").endswith("-" + str(photo_id))
    ):
        return "Не подтверждена страница фотографии Pexels."
    if (
        author.scheme != "https"
        or author.netloc != "www.pexels.com"
        or not author.path.startswith("/@")
    ):
        return "Не подтверждён автор Pexels."
    if (
        not candidate.get("author")
        or candidate.get("license") != "Pexels License"
        or candidate.get("license_url") != LICENSE_URL
        or candidate.get("terms_url") != TERMS_URL
    ):
        return "Отсутствуют автор или действительные условия Pexels."
    if any(
        type(candidate.get(k)) is not int or candidate[k] <= 0
        for k in ("original_width", "original_height")
    ):
        return "Некорректные размеры Pexels."
    if (
        candidate.get("original_mime") not in {"image/jpeg", "image/png"}
        or max(candidate["original_width"], candidate["original_height"]) < 1080
    ):
        return "Непригодный формат/разрешение фотографии Pexels."
    try:
        image_url(candidate["original_url"], photo_id)
        image_url(candidate["thumbnail_url"], photo_id)
    except (KeyError, ValueError, ProductionError):
        return "Неподтверждённый URL файла Pexels."
    return None


def verify_source(candidate, root):
    problem = metadata_problem(candidate)
    if problem:
        raise ProductionError(problem)
    evidence = candidate.get("rights_evidence")
    if (
        not isinstance(evidence, dict)
        or not evidence.get("snapshot_path")
        or not evidence.get("snapshot_sha256")
    ):
        raise ProductionError("Нет сохранённого подтверждения происхождения Pexels.")
    path = inside(root, evidence["snapshot_path"])
    if sha(path) != evidence["snapshot_sha256"]:
        raise ProductionError("Изменён снимок API Pexels.")
    photo = next(
        (
            p
            for p in read(path).get("photos", [])
            if p.get("id") == candidate["pexels_id"]
        ),
        None,
    )
    if (
        not photo
        or any(
            photo.get(k) != candidate.get(v)
            for k, v in [
                ("photographer", "author"),
                ("photographer_url", "photographer_url"),
                ("url", "source_page"),
                ("width", "original_width"),
                ("height", "original_height"),
            ]
        )
        or photo.get("src", {}).get("original") != candidate["original_url"]
    ):
        raise ProductionError(
            "Происхождение Pexels не совпадает с сохранённым ответом API."
        )


def search(gateway, query):
    """Одна страница. Ошибки и квота дают fallback без повторов/ожидания сброса."""
    journal = gateway.journal
    metrics = journal.state.setdefault("metrics", {})
    state = journal.state.setdefault("pexels", {})
    key = (
        dotenv_values(ROOT / ".env", interpolate=False).get("PEXELS_API_KEY") or ""
    ).strip()
    if not key:
        state.update(
            status="not_configured",
            reason="PEXELS_API_KEY отсутствует; используется Wikimedia.",
        )
        journal.save()
        return None
    identity = {"provider": "pexels", "query": query, "per_page": 10, "page": 1}
    operation = digest({"step": "pexels_search", "identity": identity})
    existing = journal.state["operations"].get(operation)
    if existing:
        metrics["pexels_cache_hits"] = metrics.get("pexels_cache_hits", 0) + 1
        # Не повторяем даже прерванный запрос: разрешён fallback Commons.
        if existing["status"] != "completed":
            return None
    elif state.get("disabled"):
        return None
    elif (
        gateway.budget["calls"]
        >= gateway.settings["max_search_api_calls_per_fragment_per_round"]
    ):
        return None

    def request():
        gateway.budget["calls"] += 1
        metrics["pexels_http_attempts"] = metrics.get("pexels_http_attempts", 0) + 1
        state["status"] = "requesting"
        journal.save()
        quota = {}
        content = bytearray()
        started = time.monotonic()
        try:
            with gateway.client.stream(
                "GET",
                API_URL,
                params={"query": query, "per_page": 10, "page": 1},
                headers={"Authorization": key},
                timeout=TIMEOUT,
                follow_redirects=False,
            ) as response:
                quota = {
                    name: response.headers.get(name)
                    for name in (
                        "X-Ratelimit-Limit",
                        "X-Ratelimit-Remaining",
                        "X-Ratelimit-Reset",
                        "Retry-After",
                    )
                }
                state["quota"] = quota
                if response.status_code != 200:
                    state.update(
                        status="fallback",
                        reason=f"Pexels HTTP {response.status_code}",
                        disabled=True,
                    )
                    return {"candidates": [], "reason": state["reason"]}
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if (
                        len(content) > 2 * 1024 * 1024
                        or time.monotonic() - started > TIMEOUT
                    ):
                        raise ValueError("size/deadline")
            data = json.loads(content)
            if not isinstance(data, dict) or not isinstance(data.get("photos"), list):
                raise ValueError("shape")
            folder = journal.directory / "visual_materials" / "pexels"
            folder.mkdir(parents=True, exist_ok=True)
            snapshot = folder / (digest(identity) + ".json")
            atomic_write_json(snapshot, data)
            candidates = []
            for rank, photo in enumerate(data["photos"][:10], 1):
                try:
                    src = photo["src"]
                    original = src["original"]
                    mime = (
                        "image/png"
                        if urlsplit(original).path.lower().endswith(".png")
                        else "image/jpeg"
                    )
                    candidate = {
                        "origin": "pexels",
                        "pexels_id": photo["id"],
                        "title": photo.get("alt") or "Pexels photo",
                        "description": photo.get("alt", ""),
                        "author": photo["photographer"],
                        "photographer_url": photo["photographer_url"],
                        "source_page": photo["url"],
                        "provider_url": "https://www.pexels.com/",
                        "license": "Pexels License",
                        "license_url": LICENSE_URL,
                        "terms_url": TERMS_URL,
                        "credit": f"Photo by {photo['photographer']} on Pexels",
                        "original_url": original,
                        "thumbnail_url": src["medium"],
                        "original_mime": mime,
                        "original_width": photo["width"],
                        "original_height": photo["height"],
                        "width": photo["width"],
                        "height": photo["height"],
                        "search_rank": rank,
                        "restrictions": "",
                        "categories": [],
                        "rights_evidence": {
                            "method": "Pexels official search API",
                            "snapshot_path": str(
                                snapshot.relative_to(journal.directory)
                            ),
                            "snapshot_sha256": sha(snapshot),
                            "source_page": photo["url"],
                            "received_at_utc": datetime.now(timezone.utc).isoformat(),
                        },
                    }
                    if not metadata_problem(candidate):
                        candidates.append(candidate)
                except (KeyError, TypeError, ValueError):
                    continue
            state.update(status="available" if candidates else "empty", reason=None)
            if quota.get("X-Ratelimit-Remaining") == "0":
                state.update(
                    disabled=True,
                    reason="Квота Pexels исчерпана; дальнейший поиск через Wikimedia.",
                )
            return {
                "candidates": candidates,
                "provider": "pexels",
                "continue": {},
                "quota": quota,
            }
        except (httpx.HTTPError, ValueError, TypeError):
            state.update(
                status="fallback",
                reason="Pexels: сеть, таймаут или некорректный ответ; используется Wikimedia.",
                disabled=True,
            )
            return {"candidates": [], "reason": state["reason"]}
        finally:
            journal.save()

    page = journal.operation("pexels_search", identity, request)
    return page if page.get("candidates") else None


def search_first(
    gateway, query, continuation=None, *, limit=10, side=3072, force_commons=False
):
    from commons_materials import search_files

    if not force_commons and not continuation and limit >= 10:
        page = search(gateway, query)
        if page:
            return page
    return dict(
        search_files(gateway, query, continuation, limit=limit, side=side),
        provider="wikimedia",
    )


def fetch_material(candidate, gateway, download_kind="finalist"):
    """Берём src.original: без сервисного кропа. Старые ограничения размера/декодирования действуют."""
    verify_source(candidate, gateway.journal.directory)
    folder = gateway.journal.directory / "visual_materials" / "originals"
    folder.mkdir(parents=True, exist_ok=True)
    extension = ".png" if candidate["original_mime"] == "image/png" else ".jpg"
    path = folder / (candidate["candidate_id"] + extension)
    result = download_file(
        candidate, gateway, candidate["original_url"], path, download_kind
    )
    measured = inspect_image(path.read_bytes(), minimum_width=1, minimum_height=1)
    expected = "PNG" if extension == ".png" else "JPEG"
    if result["mime"] != candidate["original_mime"] or measured["format"] != expected:
        raise ProductionError("Формат файла Pexels не совпадает с метаданными.")
    return dict(
        candidate,
        **measured,
        original_path=str(path.relative_to(gateway.journal.directory)),
        used_url=candidate["original_url"],
    )


def download_file(candidate, gateway, url, destination, kind):
    """Сбой CDN Pexels разрешает резерв Commons; сбои OpenAI сюда не попадают."""
    from commons_requests import CommonsPaused

    state = gateway.journal.state.setdefault("pexels", {})
    if state.get("download_unavailable_reason"):
        raise ProductionError(state["download_unavailable_reason"])
    previous = gateway.client.timeout
    try:
        gateway.client.timeout = httpx.Timeout(TIMEOUT)
        return gateway.get(
            {},
            kind=kind,
            url=image_url(url, candidate["pexels_id"]),
            destination=destination,
        )
    except CommonsPaused as error:
        message = str(error).replace("Wikimedia", "Pexels CDN")
        state.update(
            disabled=True, status="fallback", download_unavailable_reason=message
        )
        gateway.journal.save()
        raise ProductionError(message + "; используется резерв Wikimedia.") from None
    finally:
        gateway.client.timeout = previous
