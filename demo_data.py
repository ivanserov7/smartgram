"""Явно подставные ответы для проверки управления конвейером без сети."""

from fact_checker import VerificationDraft
from scriptwriter import ScriptDraft


DEMO_URL = "https://example.org/smartgram-demo"
DEMO_TEXTS = [
    "Почему вечером Солнце кажется красным, хотя днём его свет выглядит иначе? Всё начинается с пути света через атмосферу.",
    "Солнечный свет содержит разные цвета. По дороге к наблюдателю он проходит через воздух, который влияет на распространение света.",
    "Коротковолновый свет рассеивается молекулами воздуха сильнее длинноволнового. Поэтому разные части солнечного света проходят через атмосферу по-разному, меняя видимый цвет.",
    "Когда Солнце низко над горизонтом, путь его света через атмосферу длиннее. К наблюдателю приходит относительно больше красного света.",
    "Так красный закат связывает знакомую картину с рассеянием света: важен и состав солнечного света, и его путь через воздух.",
]


def demo_research(topic: str, case: str) -> dict:
    """Фиксированная учебная заготовка о закате; не настоящее исследование темы."""
    return {
        "topic": topic, "mode": "demo", "model": "demo-no-api", "schema_version": 2,
        "status": "insufficient_data" if case == "insufficient" else "researched",
        "explanation": "ТЕСТОВЫЕ ДАННЫЕ: учебная заготовка о закате, не результат исследования указанной темы.",
        "facts": [{"text": text, "sources": [{"url": DEMO_URL, "title": "Подставной источник"}]}
                  for text in DEMO_TEXTS],
        "sources": [{"url": DEMO_URL, "title": "Подставной источник"}],
        "warnings": [],
        "blocking_issues": ["Тест нехватки основного объяснения."] if case == "insufficient" else [],
        "topic_assessment": {"can_explain_topic": case != "insufficient", "reason": "Тестовая оценка достаточности.",
                             "core_fact_ids": [] if case == "insufficient" else [2, 3, 4]},
        "fact_check_status": "not_performed",
    }


class DemoResponses:
    """Возвращает сценарные заготовки вместо OpenAI; это не проверка фактов."""

    def __init__(self, case="success"):
        self.case = case
        self.script_calls = 0
        self.check_calls = 0


    def generate(self, schema, instructions, data):
        if schema is ScriptDraft:
            self.script_calls += 1
            fragments = [{"text": text, "visual": "Учебная схема рассеяния солнечного света в атмосфере."} for text in DEMO_TEXTS]
            if self.case == "fail" or self.case == "repair" and self.script_calls == 1:
                fragments[1]["text"] = "Закат красный на любой планете."
            return ScriptDraft(fragments=fragments)
        if schema is VerificationDraft:
            self.check_calls += 1
            issues = [{"fragment_index": index, "field": "text", "phrase": fragment["text"],
                       "problem": "ТЕСТ: утверждение не подтверждено.", "correction": "Уберите обобщение.", "source_url": None}
                      for index, fragment in enumerate(data["fragments"]) if "любой планете" in fragment["text"]]
            if self.case == "unavailable":
                issues = [{"fragment_index": 0, "field": "text", "phrase": data["fragments"][0]["text"],
                           "problem": "ТЕСТ: источник недоступен.", "correction": "Нужно доступное подтверждение.", "source_url": None}]
            return VerificationDraft(approved=not issues, issues=issues)
        raise ValueError("Неизвестный тип подставного ответа.")
