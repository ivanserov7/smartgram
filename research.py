"""Вход исследования и сохранение. Новый порядок реализован в research_engine.py."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values
from openai import APIConnectionError, APIError, APIStatusError, OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from research_policy import ASSESSMENT_INSTRUCTIONS, MAX_FACTS, SCHEMA_VERSION, TopicAssessment, research_blockers


class ResearchError(Exception):
    """Понятная ошибка исследования без секретов и содержимого HTTP-запроса."""


class FactDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1)
    source_urls: list[str]


class ResearchDraft(BaseModel):
    """Историческая схема 2 для совместимости; новый исследователь использует CollectionResult."""
    model_config = ConfigDict(extra="forbid")
    explanation: str = Field(min_length=1)
    facts: list[FactDraft] = Field(max_length=MAX_FACTS)
    warnings: list[str]
    blocking_issues: list[str]
    topic_assessment: TopicAssessment


def load_settings() -> tuple[str, str]:
    """Читает настройки только из .env рядом с кодом, не печатая значения."""
    settings = {**dotenv_values(Path(__file__).with_name(".env"), interpolate=False), **os.environ}
    api_key = (settings.get("OPENAI_API_KEY") or "").strip()
    model = (settings.get("OPENAI_MODEL") or "").strip()
    if not api_key or api_key == "your_api_key_here":
        raise ResearchError("Добавьте OPENAI_API_KEY в .env (пример: .env.example).")
    if not model:
        raise ResearchError("Укажите OPENAI_MODEL в .env.")
    return api_key, model


def require_complete_response(response, result_name="исследования") -> None:
    """Не принимает оборванный ответ или отказ модели за результат."""
    if response.status != "completed" or not response.output_text.strip():
        raise ResearchError(f"API не вернул полный текст {result_name}. Этап не завершён.")
    for item in response.output:
        if item.type == "message":
            if any(part.type == "refusal" for part in item.content):
                raise ResearchError("Модель отказалась обрабатывать тему.")


def collect_sources(response) -> list[dict]:
    """Берёт URL исключительно из метаданных поиска и цитат, не из текста модели."""
    sources = {}

    def add_source(url, title=""):
        if isinstance(url, str) and urlsplit(url).scheme in ("http", "https"):
            sources[url] = {"url": url, "title": title or url}

    for item in response.output:
        if item.type == "web_search_call":
            for source in getattr(item.action, "sources", None) or []:
                add_source(getattr(source, "url", None))
        elif item.type == "message":
            for part in item.content:
                for citation in getattr(part, "annotations", []):
                    if citation.type == "url_citation":
                        add_source(citation.url, citation.title)
    return list(sources.values())


def build_result(topic: str, model: str, search_response, draft: ResearchDraft) -> dict:
    """Исторический формат 2; его нельзя автоматически передавать новому сценаристу."""
    sources = collect_sources(search_response)
    source_by_url = {source["url"]: source for source in sources}
    facts = []
    warnings = list(draft.warnings)
    issues = list(draft.blocking_issues)
    fact_numbers = {}
    for number, fact in enumerate(draft.facts, 1):
        # Не исправляем и не достраиваем адреса: требуется точное совпадение.
        if not fact.text.strip() or not fact.source_urls or any(
            url not in source_by_url for url in fact.source_urls
        ):
            warnings.append(f"Исключён факт {number} без источников из метаданных поиска API.")
            continue
        fact_numbers[number] = len(facts) + 1
        facts.append({
            "text": fact.text,
            "sources": [source_by_url[url] for url in dict.fromkeys(fact.source_urls)],
        })
    assessment = draft.topic_assessment.model_dump()
    original_ids = assessment["core_fact_ids"]
    if (any(number not in fact_numbers for number in original_ids)
            or len(set(original_ids)) != len(original_ids)):
        issues.append("Факт основного объяснения исключён или указан некорректно; нужна переоценка достаточности.")
        assessment["can_explain_topic"] = False
    assessment["core_fact_ids"] = list(dict.fromkeys(fact_numbers[number] for number in original_ids if number in fact_numbers))
    blocking_issues = research_blockers(facts, sources, assessment, issues)
    return {
        "topic": topic,
        "model": model,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": SCHEMA_VERSION,
        "status": "insufficient_data" if blocking_issues else "researched",
        "explanation": draft.explanation,
        "facts": facts,
        "warnings": list(dict.fromkeys(warnings)),
        "blocking_issues": blocking_issues,
        "topic_assessment": assessment,
        "sources": sources,
        "fact_check_status": "not_performed",
        "search_text": search_response.output_text,
        "search_response_id": search_response.id,
    }


def research_topic(topic: str, output_root=None) -> dict:
    """Два этапа: сбор через веб-инструмент, затем оценка достаточности без веба."""
    from research_engine import run_research
    return run_research(topic, output_root=output_root)


def render_research_markdown(result: dict) -> str:
    """Создаёт читаемую версию с источниками рядом с каждым фактом."""
    if result.get("schema_version") == 4:
        from research_format import render_new_markdown
        return render_new_markdown(result)
    lines = [f"# {result['topic']}", "", result["explanation"], "", "## Факты", ""]
    if result.get("mode") == "demo":
        lines[2:2] = ["ТЕСТОВЫЕ ДАННЫЕ: не настоящее исследование и не проверка фактов.", ""]
    for number, fact in enumerate(result["facts"], start=1):
        lines.append(f"{number}. {fact['text']}")
        for source in fact["sources"]:
            lines.append(f"   - [{source['title']}]({source['url']})")
        for evidence in fact.get("evidence", []):
            lines.extend([f"   > {evidence['quote']}", f"   Источник: {evidence['url']}",
                          f"   Смысловая проверка: {evidence.get('semantic_status')}. {evidence.get('semantic_reason', '')}"])
        lines.append("")
    for field, title in (("warnings", "Предупреждения (не блокируют ролик)"),
                         ("blocking_issues", "Причины остановки исследования"),
                         ("limitations", "Исходные оговорки старого исследования (до переоценки)")):
        if result.get(field):
            lines.extend([f"## {title}", ""])
            lines.extend(f"- {item}" for item in result[field])
    if result.get("topic_assessment"):
        lines.extend(["", "## Достаточность для ответа на тему", "", result["topic_assessment"]["reason"]])
    if result.get("core_support"):
        lines.extend(["", "## Независимое подтверждение основного объяснения", "", result["core_support"]["reason"]])
    for page in result.get("source_pages", []):
        review = page.get("assessment", {})
        lines.extend(["", f"## Источник: {page.get('title', page['url'])}", "", page["url"],
                      f"Решение: {review.get('decision', 'не оценён')}. {review.get('reason', page.get('reason', ''))}",
                      f"Компетентность: {review.get('authority_reason', 'не оценивалась')}",
                      f"Тип: {review.get('material_kind', 'недоступен')}"])
    lines.extend(["", "Проверка утверждений отдельным этапом не выполнена.",
                  "Видео не создано.", ""])
    return "\n".join(lines)


def save_research(result: dict, output_root: Path) -> Path:
    """Создаёт отдельную папку запуска и сохраняет две версии результата."""
    from tempfile import mkdtemp

    output_root.mkdir(parents=True, exist_ok=True)
    prefix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_")
    run_directory = Path(mkdtemp(prefix=prefix, dir=output_root))
    (run_directory / "research.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (run_directory / "research.md").write_text(render_research_markdown(result), encoding="utf-8")
    return run_directory
