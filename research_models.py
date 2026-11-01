"""Формат нового исследования: фрагменты модели отделены от раскрытых событий инструмента."""

import ipaddress
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from research import ResearchError


class ResearchModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SourceFragment(ResearchModel):
    url: str = Field(min_length=1)
    title: str | None
    excerpt: str | None
    source_reason: str = Field(min_length=1)
    availability: Literal["page_text", "partial_text", "search_description", "unavailable", "unknown"]
    availability_details: str
    excerpt_kind: Literal["verbatim_claim", "paraphrase", "unknown"]


class CollectedFact(ResearchModel):
    statement: str = Field(min_length=1)
    evidence: list[SourceFragment] = Field(min_length=1)


class UnconfirmedCandidate(ResearchModel):
    statement: str
    reason: str
    evidence: list[SourceFragment]


class CollectionResult(ResearchModel):
    facts: list[CollectedFact]
    unconfirmed_candidates: list[UnconfirmedCandidate]
    notes: list[str]
    contradictions: list[str]


class SufficiencyResult(ResearchModel):
    enough: bool
    missing_questions: list[str]

    @model_validator(mode="after")
    def check_verdict(self):
        if self.enough and self.missing_questions:
            raise ValueError("При enough=true список недостающего должен быть пуст.")
        if not self.enough and (not self.missing_questions or any(not item.strip() for item in self.missing_questions)):
            raise ValueError("При enough=false нужны конкретные непустые вопросы.")
        return self


def valid_source_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return False
        host = parsed.hostname.casefold()
        if host == "localhost" or host.endswith((".localhost", ".local")):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return "." in host and not any(character.isspace() for character in host)
    except (TypeError, ValueError):
        return False


def url_identity(url: str) -> str:
    parsed = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if not key.casefold().startswith("utm_")]
    return urlunsplit((parsed.scheme, parsed.netloc.casefold(), parsed.path.rstrip("/"), urlencode(sorted(query)), ""))


def has_material(evidence: dict) -> bool:
    return evidence["availability"] in {"page_text", "partial_text"} and bool((evidence["excerpt"] or "").strip())


def source_list(facts: list[dict]) -> list[dict]:
    sources = {}
    for fact in facts:
        for evidence in fact["evidence"]:
            source = sources.setdefault(evidence["url"], {"url": evidence["url"], "title": evidence["title"]})
            if source["title"] is None and evidence["title"] is not None:
                source["title"] = evidence["title"]
    return list(sources.values())


def validate_new_research(result: dict, demo=False) -> None:
    """Структура и допустимость URL, без проверки дословности и порогов количества."""
    if result.get("mode") == "demo" and not demo:
        raise ResearchError("Тестовое исследование нельзя использовать как настоящее.")
    if not isinstance(result.get("topic"), str) or not result["topic"].strip():
        raise ResearchError("Некорректный research.json: topic.")
    for name in ("facts", "sources", "unconfirmed_candidates", "notes", "contradictions", "rounds", "missing_questions"):
        if not isinstance(result.get(name), list):
            raise ResearchError(f"Некорректный research.json: {name} должно быть списком.")
    for name in ("notes", "contradictions", "missing_questions"):
        if any(not isinstance(item, str) for item in result[name]):
            raise ResearchError(f"Некорректный research.json: {name} должно содержать строки.")
    execution = result.get("execution")
    if not isinstance(execution, dict) or execution.get("state") not in {"running", "completed", "failed"}:
        raise ResearchError("Некорректный research.json: execution.")
    if result.get("enough") is not None:
        try:
            SufficiencyResult(enough=result["enough"], missing_questions=result.get("missing_questions"))
        except ValueError:
            raise ResearchError("Некорректный research.json: enough / missing_questions.") from None
    if execution["state"] == "completed" and type(result.get("enough")) is not bool:
        raise ResearchError("Нет завершённой оценки достаточности.")
    if execution["state"] == "failed" and result.get("enough") is not None:
        raise ResearchError("Сбой выполнения не может иметь вердикт достаточности.")
    if not isinstance(result.get("facts"), list):
        raise ResearchError("Некорректный research.json: facts.")
    for fact in result["facts"]:
        try:
            draft = CollectedFact.model_validate({"statement": fact["statement"], "evidence": [
                {name: item[name] for name in SourceFragment.model_fields} for item in fact["evidence"]]})
        except (ValueError, KeyError, TypeError):
            raise ResearchError("Некорректный research.json: statement / evidence.") from None
        if not any(has_material(item.model_dump()) for item in draft.evidence):
            raise ResearchError("Факт без доступного фрагмента должен быть неподтверждённым кандидатом.")
        if any(not valid_source_url(item.url) for item in draft.evidence):
            raise ResearchError("Некорректный research.json: недопустимый URL.")
        if not draft.statement.strip() or any(not item.source_reason.strip() for item in draft.evidence):
            raise ResearchError("Некорректный research.json: пустое утверждение или причина выбора источника.")


def as_script_research(result: dict) -> dict:
    """Адаптер старого интерфейса сценариста. Это не проверка истинности собранных утверждений."""
    if result.get("schema_version") != 4:
        return result
    validate_new_research(result)
    if result["execution"]["state"] != "completed" or result["enough"] is not True:
        raise ResearchError("Сценарий запрещён: нет результата «Хватает».")
    return {"topic": result["topic"], "mode": result.get("mode", "real"), "status": "researched",
            "facts": [{"text": fact["statement"], "evidence": fact["evidence"], "sources": [
                {"url": item["url"], "title": item["title"] or item["url"]} for item in fact["evidence"] if has_material(item)]}
                for fact in result["facts"]],
            "warnings": result.get("notes", []) + result.get("contradictions", []), "blocking_issues": [],
            "explanation": "Материал собран исследователем; проверка готового сценария выполняется по страницам отдельно."}
