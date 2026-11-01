"""Один локальный HTML по постоянному набору тем, со всеми входами/выходами запросов."""

import json
from pathlib import Path

from file_io import atomic_write
from research_format import verdict
from review_gallery import escape, link


AVAILABILITY = {"page_text": "текст страницы", "partial_text": "часть материала", "search_description": "только поисковое описание",
                "unavailable": "недоступен", "unknown": "неизвестно"}


def details(label: str, value) -> str:
    return f'<details><summary>{escape(label)}</summary><pre>{escape(json.dumps(value, ensure_ascii=False, indent=2))}</pre></details>'


def journal_for(result):
    directory = result.get("run_directory")
    path = Path(directory) / "research_usage.json" if directory else None
    return json.loads(path.read_text())["requests"] if path and path.exists() else []


def usage_total(requests, name):
    values = [(item.get("usage") or {}).get(name) for item in requests]
    if not values or any(value is None for value in values):
        return "—"
    return sum(values)


def render_fact(fact):
    evidence = []
    for item in fact["evidence"]:
        evidence.append('<div class="evidence">' + link(item["url"], item["title"] or item["url"]) +
                        f'<p>Основание выбора: {escape(item["source_reason"])}</p>'
                        f'<p>Доступность <strong>по заявлению модели</strong>: {AVAILABILITY[item["availability"]]} — {escape(item["availability_details"])}</p>'
                        f'<blockquote>{escape(item["excerpt"])}</blockquote>'
                        f'<p>Тип фрагмента по заявлению модели: {escape(item["excerpt_kind"])}. Дословность Python не проверял.</p>'
                        + details("Наблюдения API о чтении (отдельно от слов модели)", item.get("api_observation")) + '</div>')
    return '<article><h3>' + escape(fact["statement"]) + '</h3>' + ''.join(evidence) + '</article>'


def render_batch(results: list[dict], journals=None) -> str:
    rows, sections = [], []
    for number, result in enumerate(results, 1):
        requests = journals[number - 1]["requests"] if journals is not None else journal_for(result)
        execution = result["execution"]
        missing = '; '.join(result.get("missing_questions", []))
        error = execution.get("error")
        rows.append(f'<tr><td><a href="#topic-{number}">{escape(result["topic"])}</a></td><td>{verdict(result)}</td>'
                    f'<td>{escape(missing or "—")}</td><td>{len(result.get("rounds", []))}</td><td>{len(result["facts"])}</td>'
                    f'<td>{len(result["sources"])}</td><td>{escape(execution.get("duration_seconds") if execution.get("duration_seconds") is not None else "—")}</td>'
                    f'<td>{usage_total(requests, "input_tokens")}</td><td>{usage_total(requests, "output_tokens")}</td><td>—</td></tr>')
        request_sections = []
        for record in requests:
            request_sections.append(f'<article><h3>Запрос {record["number"]} · {escape(record["stage"])} · раунд {record["round"]}</h3>'
                f'<p>Статус выполнения: {escape(record["status"])}. Модель из ответа: {escape(record.get("model") or "—")}. '
                f'Response ID: {escape(record.get("response_id") or "—")}</p>'
                f'<p>Начало: {escape(record["started_at_utc"])}. Конец: {escape(record.get("ended_at_utc"))}. '
                f'Время: {escape(record.get("duration_seconds"))} с.</p>'
                + details("Весь фактический запрос: инструкции, вход, JSON Schema и настройки", record["request"])
                + details("Настройки SDK: timeout и запрет повторов", record["transport"])
                + details("Полный ответ API", record.get("response"))
                + details("Раскрытые веб-действия, поисковые запросы, источники и цитирования API", record.get("web"))
                + details("Usage", record.get("usage"))
                + details("Ошибка выполнения (не вердикт о материале)", record.get("error")) + '</article>')
        sections.append(f'<section id="topic-{number}"><h2>{escape(result["topic"])}</h2><p>Достаточность: <strong>{verdict(result)}</strong>.</p>'
            f'<p>Чего не хватает: {escape(missing or "—")}</p>'
            + (f'<p class="error"><strong>Сбой выполнения:</strong> {escape(error)}</p>' if error else '')
            + ''.join(render_fact(fact) for fact in result["facts"])
            + details("Оговорки и противоречия", {"notes": result.get("notes"), "contradictions": result.get("contradictions")})
            + details("Неподтверждённые кандидаты", result.get("unconfirmed_candidates"))
            + details("Раунды и конкретные недостающие вопросы", result.get("rounds"))
            + '<h2>Подробности каждого обращения к API</h2>' + ''.join(request_sections) + '</section>')
    return '''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Smartgram — исследования трёх тем</title><style>body{max-width:1300px;margin:auto;padding:28px 20px;background:#f4f6f9;color:#1e293b;font:16px/1.6 system-ui}
section,article,.notice{padding:20px;margin:20px 0;border:1px solid #d6dde7;background:white;border-radius:12px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.5 monospace}
a{color:#215e9f;overflow-wrap:anywhere}blockquote{border-left:3px solid #789;padding-left:16px}.table{overflow:auto}table{border-collapse:collapse;min-width:900px}
td,th{border:1px solid #ccd;padding:10px;text-align:left}summary{cursor:pointer;font-weight:600}details{margin:12px 0}.error{color:#a32525}</style><main>
<h1>Smartgram · исследования</h1><div class="notice">Хватает / Не хватает — оценка полноты материала, а не независимая проверка истинности.
Фрагменты предоставлены моделью и не называются автоматически проверенными цитатами. Слова модели о доступности отделены от событий веб-инструмента.
Если API не раскрыл поле, оно сохранено как null. Денежное списание не известно, поэтому указано «—».
Сценарист, озвучка и последующие этапы в этих запусках не выполнялись.</div>
<div class="table"><table><tr><th>Тема</th><th>Материал</th><th>Чего не хватает</th><th>Раунды</th><th>Факты</th><th>Источники</th><th>Секунды</th><th>Входные токены</th><th>Выходные токены</th><th>Деньги</th></tr>''' + ''.join(rows) + '</table></div>' + ''.join(sections) + '</main></html>'


def build_batch_report(results, path: Path):
    atomic_write(path, render_batch(results).encode("utf-8"))
    return path
