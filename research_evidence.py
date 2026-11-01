"""Исторический формат 3: чтение старых результатов; новый исследователь не сравнивает цитаты."""

import hashlib
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field

from model_api import StrictModel
from research import ResearchError
from research_policy import TopicAssessment


EVIDENCE_VERSION = 3


class SourceReview(StrictModel):
    url: str
    decision: Literal["accepted", "rejected"]
    reason: str
    authority_reason: str
    material_kind: str
    independence_group: str
    original_work_url: str
    derived_from_urls: list[str]


class SourceReviews(StrictModel):
    reviews: list[SourceReview]


class FactEvidence(StrictModel):
    url: str
    quote: str = Field(min_length=12)


class GroundedFact(StrictModel):
    text: str = Field(min_length=1)
    evidence: list[FactEvidence] = Field(min_length=1)


class ExtractedFacts(StrictModel):
    facts: list[GroundedFact] = Field(max_length=7)
    warnings: list[str]


class EvidenceCheck(StrictModel):
    fact_id: int
    evidence_id: int
    verdict: Literal["supported", "unsupported", "contradicted", "cannot_verify"]
    reason: str


class EvidenceChecks(StrictModel):
    checks: list[EvidenceCheck]


class ResearchAssessment(StrictModel):
    explanation: str
    warnings: list[str]
    blocking_issues: list[str]
    topic_assessment: TopicAssessment


def canonical_url(url: str) -> str:
    parsed = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parsed.query) if not key.startswith("utm_")]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), urlencode(query), ""))


def text_hash(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()


def exact_quote(quote: str, text: str) -> bool:
    return len(" ".join(quote.split())) >= 12 and " ".join(quote.split()) in " ".join(text.split())


def validate_fact_quotes(draft: ExtractedFacts, pages: list[dict]) -> tuple[list[dict], list[dict]]:
    """Один неверный URL/фрагмент не делает правильной ссылку на другое учреждение."""
    accepted = {page["url"]: page for page in pages if page.get("assessment", {}).get("decision") == "accepted"}
    facts, rejected = [], []
    for number, fact in enumerate(draft.facts, 1):
        evidence = []
        for item in fact.evidence:
            page = accepted.get(item.url)
            if page and exact_quote(item.quote, page["text"]):
                evidence.append({**item.model_dump(), "text_sha256": text_hash(page["text"])})
            else:
                rejected.append({"fact_id": number, **item.model_dump(), "reason": "Цитата отсутствует в принятом сохранённом тексте или URL не принят."})
        if evidence:
            facts.append({"text": fact.text, "evidence": evidence})
    return facts, rejected


def apply_semantic_checks(facts: list[dict], checks: EvidenceChecks) -> tuple[list[dict], list[dict]]:
    """Каждая цитата отдельно должна подтверждать ВСЁ утверждение, включая причинную связь."""
    expected = {(number, index) for number, fact in enumerate(facts, 1) for index in range(1, len(fact["evidence"]) + 1)}
    actual = [(check.fact_id, check.evidence_id) for check in checks.checks]
    if len(set(actual)) != len(actual) or set(actual) != expected:
        raise ResearchError("Проверяющий пропустил, повторил или добавил доказательства фактов.")
    by_id = {(check.fact_id, check.evidence_id): check for check in checks.checks}
    output, rejected = [], []
    for number, fact in enumerate(facts, 1):
        evidence = []
        for index, item in enumerate(fact["evidence"], 1):
            check = by_id[number, index]
            if check.verdict == "supported":
                evidence.append({**item, "semantic_status": "supported", "semantic_reason": check.reason})
            else:
                rejected.append({"fact_id": number, **item, "reason": check.reason, "verdict": check.verdict})
        if evidence:
            output.append({**fact, "evidence": evidence})
    return output, rejected


def independent_groups(urls: list[str], pages: list[dict]) -> list[list[str]]:
    """Склеивает общий оригинал и одинаковый текст; домен издателя не определяет независимость."""
    by_url = {page["url"]: page for page in pages}
    groups = []
    uncertain = []
    for url in dict.fromkeys(urls):
        page = by_url[url]
        review = page["assessment"]
        label = review["independence_group"].strip().casefold()
        generic_label = label in {"", "unknown", "original", "independent", "primary"}
        if generic_label and not review["original_work_url"]:
            uncertain.append(url)
            continue
        keys = {"text:" + text_hash(page["text"]), "url:" + canonical_url(page.get("final_url", url)),
                "url:" + canonical_url(url)}
        if not generic_label:
            keys.add("group:" + label)
        for origin in [review["original_work_url"], *review["derived_from_urls"]]:
            if origin:
                keys.add("url:" + canonical_url(origin))
        matches = [group for group in groups if group["keys"] & keys]
        merged = {"keys": set(keys), "urls": [url]}
        for group in matches:
            merged["keys"].update(group["keys"])
            merged["urls"].extend(group["urls"])
            groups.remove(group)
        groups.append(merged)
    # Неясное происхождение не прибавляет независимого подтверждения к известным.
    return [group["urls"] for group in groups] if groups else ([uncertain] if uncertain else [])


def core_support(facts: list[dict], assessment: dict, pages: list[dict]) -> dict:
    ids = assessment["core_fact_ids"]
    if (not ids or any(type(number) is not int or not 1 <= number <= len(facts) for number in ids)
            or len(set(ids)) != len(ids)):
        return {"status": "missing", "facts": [], "reason": "Нет корректных опорных фактов основного объяснения."}
    support = [{"fact_id": number, "groups": independent_groups([item["url"] for item in facts[number - 1]["evidence"]], pages)} for number in ids]
    complete = all(len(item["groups"]) >= 2 for item in support)
    return {"status": "independent" if complete else "single_source", "facts": support,
            "reason": "Основные утверждения подтверждены минимум двумя независимыми материалами." if complete else
                      "Для основного объяснения нет двух независимых подтверждений; требуется ручная проверка."}
