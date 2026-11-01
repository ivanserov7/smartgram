"""Первый шаг сценариста: один цельный текст с предварительным визуальным замыслом."""

import hashlib
import json
import re

from pydantic import Field, StrictStr, ValidationError, field_validator

from model_api import StrictModel, generate_structured
from research import ResearchError
from script_config import ScriptSettings, read_script_prompt


class ScriptFragment(StrictModel):
    text: StrictStr = Field(min_length=1)
    visual: StrictStr = Field(min_length=1)

    @field_validator("text", "visual")
    @classmethod
    def require_nonblank(cls, value):
        if not value.strip():
            raise ValueError("Нужен непустой текст.")
        return value


class ScriptDraft(StrictModel):
    fragments: list[ScriptFragment] = Field(min_length=1)


def validate_script(value: dict) -> dict:
    """Принимает только новый контракт; старым сценариям не придумывает visual."""
    try:
        return ScriptDraft.model_validate(value).model_dump()
    except ValidationError:
        raise ResearchError("Некорректный сценарий: нужен непустой fragments с полями text и visual, без лишних полей. "
                            "Старый текстовый сценарий нужно заново написать по сохранённому исследованию.") from None


def narration_text(script: dict) -> str:
    return " ".join(fragment["text"] for fragment in validate_script(script)["fragments"])


def script_identity(script: dict) -> str:
    content = json.dumps(validate_script(script), ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def estimate_duration(text: str) -> float:
    """Только справочный ориентир; число слов не служит причиной отказа."""
    return round(len(re.findall(r"\b[\w]+(?:[-’][\w]+)*\b", text)) * 60 / 140, 2)


def require_usable_research(research: dict, demo=False) -> None:
    if research.get("mode") == "demo" and not demo:
        raise ResearchError("Тестовое исследование нельзя использовать как настоящее.")
    if research.get("schema_version") == 4:
        from research_models import validate_new_research
        validate_new_research(research, demo=demo)
        if research["execution"]["state"] == "completed" and research["enough"] is True:
            return
    elif demo and research.get("status") == "researched" and not research.get("blocking_issues"):
        return
    raise ResearchError("Сценарист запускается только после enough=true («Хватает») двухшагового исследователя. "
                        "Используйте актуальное сохранённое исследование.")


def write_script(research: dict, previous=None, feedback=None, generate=generate_structured,
                 settings=None, demo=False) -> dict:
    """Тема, весь материал и прежняя версия передаются отдельно от редактируемого промпта."""
    require_usable_research(research, demo)
    settings = settings or ScriptSettings()
    data = {"topic": research["topic"], "research": research,
            "target_duration_seconds": {"min": settings.target_min_seconds, "max": settings.target_max_seconds},
            "previous": previous, "feedback": feedback}
    draft = generate(ScriptDraft, read_script_prompt(settings, "writing"), data)
    value = draft.model_dump() if isinstance(draft, ScriptDraft) else draft
    return validate_script(value)
