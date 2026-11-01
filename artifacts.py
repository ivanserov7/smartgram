"""Чтение исследования и сохранение промежуточных результатов запуска."""

import json
from pathlib import Path
from urllib.parse import urlsplit

from research import ResearchError
from research_policy import MAX_FACTS, MIN_FACTS, SCHEMA_VERSION, research_blockers


def require_text(value, field: str) -> None:
    """Сообщает имя повреждённого поля, не выводя его содержимое."""
    if not isinstance(value, str) or not value.strip():
        raise ResearchError(f"Некорректный research.json: {field} должно быть непустой строкой.")


def require_list(value, field: str) -> None:
    """Не позволяет принять строку, null или объект за список."""
    if not isinstance(value, list):
        raise ResearchError(f"Некорректный research.json: {field} должно быть списком.")


def validate_sources(sources, field: str) -> None:
    """Проверяет URL и заголовки и общего списка, и источников каждого факта."""
    require_list(sources, field)
    for index, source in enumerate(sources):
        source_field = f"{field}[{index}]"
        if not isinstance(source, dict):
            raise ResearchError(f"Некорректный research.json: {source_field} должно быть объектом.")
        require_text(source.get("url"), f"{source_field}.url")
        require_text(source.get("title"), f"{source_field}.title")
        try:
            parsed = urlsplit(source["url"])
            valid_url = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ResearchError(f"Некорректный research.json: {source_field}.url должен быть HTTP(S)-адресом.")


def validate_notes(result: dict, field: str) -> None:
    require_list(result.get(field), field)
    for index, note in enumerate(result[field]):
        require_text(note, f"{field}[{index}]")


def validate_assessment(result: dict) -> None:
    assessment = result.get("topic_assessment")
    if not isinstance(assessment, dict):
        raise ResearchError("Некорректный research.json: topic_assessment должно быть объектом.")
    if type(assessment.get("can_explain_topic")) is not bool:
        raise ResearchError("Некорректный research.json: topic_assessment.can_explain_topic должно быть true или false.")
    require_text(assessment.get("reason"), "topic_assessment.reason")
    ids = assessment.get("core_fact_ids")
    require_list(ids, "topic_assessment.core_fact_ids")
    if any(type(number) is not int or not 1 <= number <= len(result["facts"]) for number in ids) or len(set(ids)) != len(ids):
        raise ResearchError("Некорректный research.json: topic_assessment.core_fact_ids содержит неверные номера фактов.")
    blockers = research_blockers(result["facts"], result["sources"], assessment, result["blocking_issues"])
    expected = "insufficient_data" if blockers else "researched"
    if result.get("schema_version") == 3:
        from research_validation import validate_saved_evidence
        support = validate_saved_evidence(result)
        if not blockers:
            expected = "researched" if support["status"] == "independent" else "needs_manual_review" if support["status"] == "single_source" else "insufficient_data"
        if result["status"] == "needs_manual_review":
            validate_notes(result, "manual_review_reasons")
            if not result["manual_review_reasons"]:
                raise ResearchError("Некорректный research.json: manual_review_reasons должен объяснять остановку.")
    if result["status"] != expected:
        raise ResearchError("Некорректный research.json: status не согласован с blocking_issues и topic_assessment. Нужна явная переоценка.")
    if result["status"] == "insufficient_data" and not result["blocking_issues"]:
        raise ResearchError("Некорректный research.json: blocking_issues должен объяснять остановку.")


def validate_research(result: dict, demo=False, allow_legacy=False) -> None:
    """Проверяет структуру и достаточность; старый формат не получает новый статус автоматически."""
    if isinstance(result, dict) and result.get("schema_version") == 4:
        from research_models import validate_new_research
        validate_new_research(result, demo=demo)
        return
    if not isinstance(result, dict):
        raise ResearchError("Некорректный research.json: ожидается объект исследования.")
    for field in ("topic", "status", "explanation"):
        require_text(result.get(field), field)
    if result["status"] not in {"researched", "insufficient_data", "needs_manual_review"}:
        raise ResearchError("Некорректный research.json: неизвестный status.")
    if "mode" in result and (not isinstance(result["mode"], str) or result["mode"] not in {"real", "demo"}):
        raise ResearchError("Некорректный research.json: mode должен быть real или demo.")
    if not demo and result.get("mode") == "demo":
        raise ResearchError("Тестовое исследование нельзя продолжить в реальном режиме. Используйте --demo.")
    legacy = "schema_version" not in result
    if not legacy and (type(result["schema_version"]) is not int or result["schema_version"] not in {SCHEMA_VERSION, 3}):
        raise ResearchError("Некорректный research.json: неизвестный schema_version.")
    # limitations сохраняется лишь как исходная история. Его наличие не блокирует формат 2.
    if legacy or "limitations" in result:
        validate_notes(result, "limitations")
    if not legacy:
        validate_notes(result, "warnings")
        validate_notes(result, "blocking_issues")
    validate_sources(result.get("sources"), "sources")
    require_list(result.get("facts"), "facts")
    if len(result["facts"]) > MAX_FACTS or result["status"] == "researched" and len(result["facts"]) < MIN_FACTS:
        raise ResearchError("Некорректный research.json: для researched нужны 3–7 содержательных фактов с источниками.")
    source_urls = {source["url"] for source in result["sources"]}
    for index, fact in enumerate(result["facts"]):
        fact_field = f"facts[{index}]"
        if not isinstance(fact, dict):
            raise ResearchError(f"Некорректный research.json: {fact_field} должно быть объектом.")
        require_text(fact.get("text"), f"{fact_field}.text")
        validate_sources(fact.get("sources"), f"{fact_field}.sources")
        if not fact["sources"]:
            raise ResearchError(f"Некорректный research.json: {fact_field}.sources не должно быть пустым.")
        if not legacy and any(source["url"] not in source_urls for source in fact["sources"]):
            raise ResearchError(f"Некорректный research.json: {fact_field}.sources содержит URL вне общего списка sources.")
    if legacy:
        if not allow_legacy:
            raise ResearchError("Старый research.json требует явной переоценки: запустите reevaluate_research.py --research-file ПУТЬ. Исходный статус сохранён.")
        return
    validate_assessment(result)


def load_research(path: Path, demo=False, allow_legacy=False) -> dict:
    """Загружает JSON, не повторяя исследование и не меняя исходный файл."""
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise ResearchError("Не удалось прочитать research.json. Проверьте путь и формат JSON.") from None
    validate_research(result, demo, allow_legacy=allow_legacy)
    return result


def save_json(path: Path, data: dict) -> None:
    """Пишет JSON в UTF-8 для следующего этапа."""
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def render_artifact(name: str, data: dict) -> str:
    """Читаемая версия сценария, проверки, плана сцен или снимков источников."""
    lines = [f"# {name}", "", f"Режим: {data['mode']}", ""]
    if data["mode"] == "demo":
        lines.extend(["ТЕСТОВЫЕ ДАННЫЕ. Настоящая проверка фактов не выполнялась.", ""])
    for number, fragment in enumerate(data.get("fragments", []), 1):
        lines.extend([f"## Фрагмент {number}", "", fragment["text"], "", f"Что показать: {fragment['visual']}", ""])
    if "approved" in data:
        lines.extend(["Можно использовать" if data["approved"] else "Нужно исправить", ""])
    for line in data.get("lines", []):
        lines.extend([f"## Реплика {line['id']} ({line['role']})", "", line["text"], "",
                      f"Факты исследования: {line['fact_ids']}", ""])
        lines.extend(f"- {url}" for url in line["source_urls"])
    for check in data.get("checks", []):
        lines.extend(["", f"## Реплика {check['line_id']}: {check['verdict']}", "", check["reason"]])
        for evidence in check["evidence"]:
            lines.extend(["", f"> {evidence['quote']}", "", evidence["url"]])
    for issue in data.get("issues", []):
        lines.extend(["", f"- {issue}"])
    for source in data.get("unavailable_sources", []):
        lines.extend(["", f"Недоступный источник: {source['url']} — {source['reason']}"])
    for scene in data.get("scenes", []):
        lines.extend(["", f"## Сцена {scene['id']}", "", scene["narration"], "",
                      f"Визуал: {scene['visual_description']}", f"Поиск: {scene['search_query']}",
                      f"Материал: {scene['material_type']}",
                      (f"Примерная длительность: {scene['estimated_duration_seconds']} с" if scene.get('estimated_duration_seconds') is not None
                       else "Точные границы фрагмента пока не определены; сопоставление с озвучкой не реализовано.")])
    for page in data.get("pages", []):
        lines.extend(["", f"## {page['url']}", "", f"Статус: {page['status']}", "",
                      page.get("reason", ""), page["text"]])
    if "estimated_duration_seconds" in data:
        lines.extend(["", f"Оценка длительности: {data['estimated_duration_seconds']} с. Точная — после озвучки."])
    if "status" in data:
        lines.extend(["", f"Статус: {data['status']}"])
    return "\n".join(lines) + "\n"


def save_artifact(directory: Path, name: str, data: dict, mode: str) -> None:
    """Каждый артефакт помечает реальный или демонстрационный режим."""
    document = {**data, "mode": mode}
    save_json(directory / f"{name}.json", document)
    (directory / f"{name}.md").write_text(render_artifact(name, document), encoding="utf-8")
