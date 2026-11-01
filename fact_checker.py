"""Второй шаг сценариста: проверка всего текста и visual с веб-инструментом."""

from typing import Literal

from pydantic import Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator

from model_api import StrictModel, generate_structured
from research import ResearchError
from research_models import valid_source_url
from script_config import ScriptSettings, read_script_prompt
from scriptwriter import validate_script


class ScriptIssue(StrictModel):
    fragment_index: StrictInt = Field(ge=0)
    field: Literal["text", "visual"]
    phrase: StrictStr = Field(min_length=1)
    problem: StrictStr = Field(min_length=1)
    correction: StrictStr = Field(min_length=1)
    source_url: StrictStr | None

    @field_validator("phrase", "problem", "correction")
    @classmethod
    def require_nonblank(cls, value):
        if not value.strip():
            raise ValueError("Замечание должно быть конкретным.")
        return value

    @field_validator("source_url")
    @classmethod
    def require_url(cls, value):
        if value is not None and not valid_source_url(value):
            raise ValueError("Недопустимая ссылка источника.")
        return value


class VerificationDraft(StrictModel):
    approved: StrictBool
    issues: list[ScriptIssue]

    @model_validator(mode="after")
    def consistent_verdict(self):
        if self.approved != (not self.issues):
            raise ValueError("approved=true требует пустого issues; false — конкретных замечаний.")
        return self


def validate_verification(value: dict, script: dict) -> dict:
    try:
        draft = VerificationDraft.model_validate(value)
        for issue in draft.issues:
            if issue.fragment_index >= len(script["fragments"]):
                raise ValueError
            if issue.phrase not in script["fragments"][issue.fragment_index][issue.field]:
                raise ValueError
        return draft.model_dump()
    except (ValueError, KeyError, TypeError):
        raise ResearchError("Некорректный ответ проверяющего: approved/issues, индекс, поле или точная проблемная фраза не соответствуют сценарию.") from None


def check_script(script: dict, research: dict, generate=generate_structured, settings=None) -> dict:
    """Не проверяет длительность по числу слов и не переписывает текст за автора."""
    script = validate_script(script)
    settings = settings or ScriptSettings()
    instructions = read_script_prompt(settings, "verification") + (
        '\nВерни JSON {"approved":bool,"issues":[{"fragment_index":0,"field":"text|visual",'
        '"phrase":"дословная фраза","problem":"проблема","correction":"что исправить","source_url":null}]}. '
        "Индексы начинаются с 0. approved=true только при пустом issues; false требует конкретных замечаний."
    )
    draft = generate(VerificationDraft, instructions, {"topic": research["topic"], "fragments": script["fragments"], "research": research})
    value = draft.model_dump() if isinstance(draft, VerificationDraft) else draft
    return validate_verification(value, script)
