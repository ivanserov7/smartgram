"""Ровно два смысловых этапа: веб-сбор материала и оценка достаточности без веб-инструмента."""

import json
import time
from copy import deepcopy
from pathlib import Path
from tempfile import mkdtemp

from file_io import atomic_write_json
from research import ResearchError
from research_models import has_material, source_list, url_identity, valid_source_url, validate_new_research
from research_requests import ResearchClient, now_iso


class ResearchRunError(ResearchError):
    def __init__(self, message, directory):
        super().__init__(f"{message} Результаты: {directory}")
        self.directory = directory


def accumulated_material(result: dict) -> dict:
    """Первый раунд получает пустые коллекции. Последующие — весь накопленный материал."""
    return {name: deepcopy(result[name]) for name in ("facts", "sources", "unconfirmed_candidates", "notes", "contradictions")}


def tool_observation(url: str, record: dict) -> dict:
    """Не приравнивает слова модели о чтении к событию API или успешному получению текста."""
    web = record.get("web") or {}
    actions = []
    for call in web.get("calls", []):
        action = call.get("action") or {}
        if action.get("type") == "open_page" and isinstance(action.get("url"), str) and url_identity(action["url"]) == url_identity(url):
            actions.append({"call_id": call.get("id"), "status": call.get("status"), "action": action})
    return {"open_page_actions": actions, "successful_text_read_confirmed": None,
            "note": "Сохранены только раскрытые API действия. Успешность чтения и тело страницы отдельно не подтверждены."}


def append_unique(target: list, values: list):
    for value in values:
        if value not in target:
            target.append(value)


def merge_collection(result: dict, draft, record: dict, round_number: int):
    """Сохраняет полезное из первого раунда, новые доказательства и все оговорки второго."""
    append_unique(result["notes"], draft.notes)
    append_unique(result["contradictions"], draft.contradictions)
    for candidate in draft.unconfirmed_candidates:
        item = candidate.model_dump()
        if any(not valid_source_url(evidence["url"]) for evidence in item["evidence"]):
            raise ResearchError("Сбор вернул недопустимый URL неподтверждённого кандидата.")
        append_unique(result["unconfirmed_candidates"], [{**item, "round": round_number}])
    for fact in draft.facts:
        evidence = []
        for fragment in fact.evidence:
            item = fragment.model_dump()
            if not valid_source_url(item["url"]):
                raise ResearchError("Сбор вернул недопустимый URL факта.")
            item["api_observation"] = tool_observation(item["url"], record)
            item["round"] = round_number
            if has_material(item):
                evidence.append(item)
            else:
                result["unconfirmed_candidates"].append({"statement": fact.statement, "evidence": [item],
                    "reason": "Модель не предоставила доступный фрагмент страницы: сниппет/неизвестность/недоступность не являются подтверждением.", "round": round_number})
        if not evidence:
            continue
        existing = next((item for item in result["facts"] if " ".join(item["statement"].casefold().split()) == " ".join(fact.statement.casefold().split())), None)
        if existing is None:
            result["facts"].append({"statement": fact.statement, "evidence": evidence})
        else:
            append_unique(existing["evidence"], evidence)
    result["sources"] = source_list(result["facts"])


def save_progress(directory: Path, result: dict):
    from research_format import render_new_markdown
    atomic_write_json(directory / "research.json", result)
    (directory / "research.md").write_text(render_new_markdown(result), encoding="utf-8")
    atomic_write_json(directory / "run.json", {"mode": result["mode"], "topic": result["topic"], "stage": "research",
                                               "execution": result["execution"], "enough": result["enough"],
                                               "missing_questions": result["missing_questions"]})


def run_research(topic: str, output_root=None, client_factory=ResearchClient) -> dict:
    if not isinstance(topic, str) or not topic.strip():
        raise ResearchError("Тема не должна быть пустой.")
    root = Path(output_root or Path(__file__).parent / "outputs")
    root.mkdir(parents=True, exist_ok=True)
    directory = Path(mkdtemp(prefix="research_", dir=root))
    result = {"schema_version": 4, "mode": "real", "topic": topic, "created_at_utc": now_iso(),
              "run_directory": str(directory.resolve()), "facts": [], "sources": [], "unconfirmed_candidates": [],
              "notes": [], "contradictions": [], "enough": None, "missing_questions": [], "rounds": [],
              "execution": {"state": "running", "error": None, "duration_seconds": None}, "settings": None}
    print(f"Папка исследования: {directory}", flush=True)
    started = time.monotonic()
    api = None
    save_progress(directory, result)
    try:
        api = client_factory(directory)
        result["settings"] = api.settings.to_dict()
        for number in range(1, api.settings.max_rounds + 1):
            round_record = {"number": number, "enough": None, "missing_questions": [], "collection_request": None, "assessment_request": None}
            result["rounds"].append(round_record)
            save_progress(directory, result)
            print(f"Раунд {number}/{api.settings.max_rounds}: сбор фактов через веб-инструмент…", flush=True)
            draft, request = api.collect(topic, accumulated_material(result), result["missing_questions"], number)
            round_record["collection_request"] = request["number"]
            merge_collection(result, draft, request, number)
            save_progress(directory, result)
            print("Оценка достаточности без веб-инструмента…", flush=True)
            assessment, request = api.assess(topic, accumulated_material(result), number)
            if assessment.enough and not result["facts"]:
                raise ResearchError("Оценка «Хватает» не согласована с отсутствием фактов с доступными фрагментами.")
            result.update(enough=assessment.enough, missing_questions=assessment.missing_questions)
            round_record.update(assessment_request=request["number"], enough=assessment.enough, missing_questions=assessment.missing_questions)
            atomic_write_json(directory / f"round_{number}.json", {"round": round_record, "accumulated_result": accumulated_material(result)})
            save_progress(directory, result)
            print("Хватает" if result["enough"] else "Не хватает: " + "; ".join(result["missing_questions"]), flush=True)
            if result["enough"]:
                break
        result["execution"].update(state="completed", duration_seconds=round(time.monotonic() - started, 3))
        validate_new_research(result)
        save_progress(directory, result)
        return result
    except (ResearchError, OSError) as error:
        message = str(error) if isinstance(error, ResearchError) else "Ошибка чтения или сохранения результатов."
        result.update(enough=None, missing_questions=[])
        result["execution"].update(state="failed", error=message, duration_seconds=round(time.monotonic() - started, 3))
        save_progress(directory, result)
        raise ResearchRunError(message, directory) from None
    finally:
        if api:
            api.close()
