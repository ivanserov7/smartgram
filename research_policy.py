"""Исторические правила форматов 2/3. Исследователь формата 4 не использует эти пороги."""

from pydantic import BaseModel, ConfigDict, Field


SCHEMA_VERSION = 2
MIN_FACTS = 3
MAX_FACTS = 7
ASSESSMENT_INSTRUCTIONS = """
Оцени достаточность именно для общего объяснения исходной темы за 30–45 секунд.
Нужны 3–7 содержательных фактов; не добавляй второстепенные сведения ради числа.
Одного количества недостаточно: topic_assessment.can_explain_topic означает,
что факты с пригодными источниками позволяют достоверно объяснить основной механизм.
В topic_assessment.reason обоснуй ответ на тему, в core_fact_ids укажи номера фактов
основного объяснения (нумерация с 1). Если объяснить нельзя, укажи false и причину.
warnings — оговорки и сомнения о необязательных деталях: их можно исключить из сценария.
Отсутствие региональных данных, спектральных измерений, количественной оценки пыли,
влажности и других второстепенных факторов само по себе НЕ блокирует общий ролик.
blocking_issues — только существенные препятствия: нет материала для основного
объяснения, нет пригодных источников или центральное утверждение противоречиво
и нельзя написать достоверный сценарий. Не скрывай настоящие препятствия в warnings.
Это оценка достаточности материала, а не отдельная проверка утверждений по страницам.
"""


class TopicAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    can_explain_topic: bool
    reason: str = Field(min_length=1)
    core_fact_ids: list[int]


def research_blockers(facts: list, sources: list, assessment: dict, issues: list[str]) -> list[str]:
    """Сочетает смысловую оценку модели с обязательными локальными условиями."""
    blockers = list(issues)
    if not MIN_FACTS <= len(facts) <= MAX_FACTS:
        blockers.append(f"Нужны {MIN_FACTS}–{MAX_FACTS} содержательных фактов с источниками; сохранено {len(facts)}.")
    if not sources:
        blockers.append("Нет пригодных источников для основного объяснения.")
    if not assessment["can_explain_topic"]:
        blockers.append("Недостаточно материала для ответа на тему: " + assessment["reason"])
    elif not assessment["core_fact_ids"]:
        blockers.append("Не указаны факты, на которых строится основное объяснение темы.")
    return list(dict.fromkeys(blockers))
