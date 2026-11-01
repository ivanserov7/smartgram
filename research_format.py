"""Читаемая версия двухэтапного исследования: фрагмент модели не называется проверенной цитатой."""


def verdict(result: dict) -> str:
    if result["execution"]["state"] != "completed" or result.get("enough") is None:
        return "—"
    return "Хватает" if result["enough"] else "Не хватает"


def render_new_markdown(result: dict) -> str:
    lines = [f"# {result['topic']}", "", f"Достаточность: {verdict(result)}", "",
             "Это оценка полноты материала, а не независимая проверка истинности фактов.", ""]
    if result["execution"].get("error"):
        lines += ["## Сбой выполнения", "", result["execution"]["error"], ""]
    if result.get("missing_questions"):
        lines += ["## Чего не хватает", "", *[f"- {item}" for item in result["missing_questions"]], ""]
    for number, fact in enumerate(result["facts"], 1):
        lines += [f"## Факт {number}", "", fact["statement"], ""]
        for evidence in fact["evidence"]:
            lines += [evidence["url"], f"Название по данным модели: {evidence['title'] or 'неизвестно'}",
                      f"Доступность по данным модели: {evidence['availability']}", "",
                      f"> {evidence['excerpt']}", "", f"Выбор источника: {evidence['source_reason']}",
                      "Фрагмент предоставлен моделью; дословность программой не проверялась.", ""]
    for field, label in (("notes", "Оговорки"), ("contradictions", "Противоречия")):
        if result[field]:
            lines += [f"## {label}", "", *[f"- {item}" for item in result[field]], ""]
    lines += ["Проверка готового сценария по первоисточникам — отдельный следующий этап.", ""]
    return "\n".join(lines)
