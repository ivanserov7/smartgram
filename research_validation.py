"""Проверка исторических снимков формата 3. Для формата 4 используется research_models.py."""

from research import ResearchError
from research_evidence import core_support, exact_quote, text_hash


def validate_saved_evidence(result: dict) -> dict:
    pages = result.get("source_pages")
    if not isinstance(pages, list):
        raise ResearchError("Некорректный research.json: source_pages должно быть списком снимков страниц.")
    by_url = {}
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("url"), str) or page["url"] in by_url:
            raise ResearchError("Некорректный research.json: повреждённые или повторённые source_pages.")
        if page.get("status") not in {"available", "unavailable"}:
            raise ResearchError("Некорректный research.json: source_pages.status неизвестен.")
        if page["status"] == "available":
            if not isinstance(page.get("text"), str) or page.get("text_sha256") != text_hash(page["text"]):
                raise ResearchError("Некорректный research.json: снимок текста повреждён или его хеш изменился.")
        review = page.get("assessment")
        if not isinstance(review, dict) or review.get("decision") not in {"accepted", "rejected"}:
            raise ResearchError("Некорректный research.json: нет оценки конкретной страницы.")
        for field in ("reason", "authority_reason", "material_kind", "independence_group", "original_work_url"):
            if review["decision"] == "accepted" and not isinstance(review.get(field), str):
                raise ResearchError(f"Некорректный research.json: оценка страницы не содержит {field}.")
        if review["decision"] == "accepted" and not isinstance(review.get("derived_from_urls"), list):
            raise ResearchError("Некорректный research.json: derived_from_urls должно быть списком.")
        if review["decision"] == "accepted" and any(not isinstance(url, str) for url in review["derived_from_urls"]):
            raise ResearchError("Некорректный research.json: derived_from_urls должен содержать строки.")
        by_url[page["url"]] = page
    for fact in result["facts"]:
        evidence = fact.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ResearchError("Некорректный research.json: факт без evidence.")
        for item in evidence:
            if not isinstance(item, dict):
                raise ResearchError("Некорректный research.json: evidence должно содержать объекты.")
            page = by_url.get(item.get("url"))
            if (not page or page["status"] != "available" or page["assessment"]["decision"] != "accepted"
                    or not isinstance(item.get("quote"), str) or not exact_quote(item["quote"], page["text"])
                    or item.get("text_sha256") != page["text_sha256"] or item.get("semantic_status") != "supported"):
                raise ResearchError("Некорректный research.json: цитата отсутствует в принятом тексте или не прошла смысловую проверку.")
        if {source["url"] for source in fact["sources"]} != {item["url"] for item in evidence}:
            raise ResearchError("Некорректный research.json: ссылки факта не согласованы с evidence.")
    return core_support(result["facts"], result["topic_assessment"], pages)
