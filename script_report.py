"""Автономный отчёт сценариста: фрагменты, история, происхождение и точные запросы."""

import json
from pathlib import Path

from file_io import atomic_write
from research_batch_report import details, usage_total
from review_gallery import escape, link
from scriptwriter import narration_text


def read_optional(directory, name):
    path = directory / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def result_label(state):
    if state.get("status") == "approved":
        return "Можно использовать"
    if state.get("status") == "needs_revision":
        return "Нужно исправить"
    return "Технический сбой" if state.get("status") == "technical_error" else "Проверка не завершена"


def fragments_table(script):
    rows = ''.join(f'<tr><td>{number}</td><td>{escape(item["text"])}</td><td>{escape(item["visual"])}</td></tr>'
                   for number, item in enumerate(script.get("fragments", []), 1))
    return '<div class="table"><table><tr><th>№ фрагмента</th><th>Текст для диктора</th><th>Что показать</th></tr>' + rows + '</table></div>'


def render_issues(issues):
    return '<ul>' + ''.join(f'<li>Фрагмент {issue["fragment_index"] + 1}, {escape(issue["field"])}: '
        f'«{escape(issue["phrase"])}». {escape(issue["problem"])} Исправление: {escape(issue["correction"])} '
        + (link(issue["source_url"], "Источник") if issue.get("source_url") else 'Источник не указан.') + '</li>' for issue in issues) + '</ul>'


def render_script_report(entries: list[dict]) -> str:
    rows, sections = [], []
    for number, entry in enumerate(entries, 1):
        directory = Path(entry["directory"]) if entry.get("directory") else None
        state = read_optional(directory, "script_state.json") if directory else {}
        research = read_optional(directory, "research.json") if directory else {}
        origin = read_optional(directory, "research_origin.json") if directory else {}
        requests = read_optional(directory, "script_usage.json").get("requests", []) if directory else []
        versions = state.get("versions", [])
        script = versions[-1]["script"] if versions else {}
        label = result_label(state) if state else "Технический сбой"
        reason = state.get("stop_reason") or entry.get("error") or "—"
        elapsed = state.get("duration_seconds")
        # Тестовые ответы никогда не называем настоящей проверкой по веб-источникам.
        mode = "ТЕСТ: подставные ответы; факты не проверялись" if state.get("mode") == "demo" else "Настоящий API"
        rows.append(f'<tr><td><a href="#topic-{number}">{escape(entry["topic"])}</a></td><td>{label}</td>'
                    f'<td>{state.get("revisions_used", 0)}</td><td>{len(requests)}</td><td>{escape(elapsed)}</td>'
                    f'<td>{usage_total(requests, "input_tokens")}</td><td>{usage_total(requests, "output_tokens")}</td><td>—</td></tr>')
        history = []
        for version in versions:
            review = version.get("verification")
            status = ("Можно использовать" if review["approved"] else "Нужно исправить") if review else "Проверка не завершена"
            history.append(f'<details><summary>Версия {version["number"]} · {status}</summary>' + fragments_table(version["script"])
                           + render_issues(review["issues"] if review else [])
                           + details("Полная версия и полученные замечания", version) + '</details>')
        calls = []
        for record in requests:
            calls.append(f'<details><summary>Запрос {record["number"]} · {escape(record["stage"])} · '
                         f'{escape(record.get("model") or record["request"]["model"])} · {escape(record["status"])}</summary>'
                         f'<p>Response ID: {escape(record.get("response_id"))}. Начало: {escape(record.get("started_at_utc"))}. '
                         f'Конец: {escape(record.get("ended_at_utc"))}. Время: {escape(record.get("duration_seconds"))} с.</p>'
                         + details("Полный отправленный запрос: промпт, формат, данные, настройки", record["request"])
                         + details("SDK: тайм-аут и технические повторы", record.get("transport"))
                         + details("Полный ответ API", record.get("response"))
                         + details("Раскрытые веб-действия и источники (URL не равен событию открытия)", record.get("web"))
                         + details("Usage", record.get("usage")) + details("Ошибка", record.get("error")) + '</details>')
        sources = ''.join('<li>' + link(item["url"], item.get("title") or item["url"]) + '</li>' for item in research.get("sources", []))
        settings = state.get("settings", {})
        sections.append(f'<section id="topic-{number}"><h2>{escape(entry["topic"])}</h2><p><strong>{label}</strong> · {mode}</p>'
                        f'<p>Причина остановки: {escape(reason)}</p>'
                        f'<p>Цель: {settings.get("target_min_seconds", 30)}–{settings.get("target_max_seconds", 45)} с. '
                        'Аудио не создавалось; точные таймкоды и длительность не измерены. Изображения ещё не подобраны.</p>'
                        + fragments_table(script) + '<h3>Связный текст озвучки</h3>'
                        f'<pre>{escape(narration_text(script) if script else "Сценарий не получен.")}</pre>'
                        f'<h3>Источники исследования</h3><ul>{sources}</ul>'
                        + details("Происхождение исследования: путь/ID и дата", origin)
                        + details("Исходное исследование целиком (включая оговорки и фрагменты)", research)
                        + '<h3>Все версии и замечания</h3>' + ''.join(history)
                        + details("Состояние, бюджет доработок и причина остановки", state)
                        + '<h3>Фактические обращения к API</h3>' + ''.join(calls) + '</section>')
    return '''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Smartgram — сценарист трёх тем</title><style>body{max-width:1300px;margin:auto;padding:28px 20px;background:#f4f6f9;color:#1e293b;font:16px/1.6 system-ui}
section,.notice{padding:22px;margin:22px 0;border:1px solid #d6dde7;background:white;border-radius:12px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.6 monospace}
a{color:#215e9f;overflow-wrap:anywhere}.table{overflow:auto}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccd;padding:10px;text-align:left;vertical-align:top}details{margin:15px 0}summary{cursor:pointer;font-weight:600}</style>
<main><h1>Smartgram · сценарист с визуальным замыслом</h1><div class="notice">Написание без веб-инструмента → проверка текста и visual с веб-инструментом.
Одобрение модели требует критического просмотра и не означает проверку реальных изображений. Скрытые рассуждения не запрашивались.
Деньги — «—»: фактическое списание неизвестно. Количество запросов — вызовы SDK; транспортные повторы, если включены явно, отдельно API не раскрывает.
Озвучка, подбор картинок и монтаж в этих прогонах не запускались.</div><div class="table"><table><tr><th>Тема</th><th>Итог</th><th>Доработки</th><th>Запросы SDK</th><th>Время, с</th><th>Входные токены</th><th>Выходные токены</th><th>Стоимость</th></tr>''' + ''.join(rows) + '</table></div>' + ''.join(sections) + '</main></html>'


def build_script_report(entries, path: Path) -> Path:
    atomic_write(path, render_script_report(entries).encode("utf-8"))
    return path
