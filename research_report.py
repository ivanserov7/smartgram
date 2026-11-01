"""Читаемый HTML-отчёт исследования из локальных артефактов, без API."""

import argparse
import json
from pathlib import Path

from file_io import atomic_write
from review_gallery import escape, link


def render_report(result: dict, journal: dict) -> str:
    if result.get("schema_version") == 4:
        from research_batch_report import render_batch
        return render_batch([result], journals=[journal])
    sections = []
    for page in result.get("source_pages", []):
        review = page.get("assessment", {})
        decision = {"accepted": "Принят", "rejected": "Отклонён"}.get(review.get("decision"), "Не оценён")
        sections.append(f'<article><h3>{link(page["url"], page.get("title", page["url"]))}</h3>'
                        f'<p><strong>{decision}</strong> · '
                        f'{escape(page.get("status"))}</p><p>{escape(review.get("reason", page.get("reason", "")))}</p>'
                        f'<p>Компетентность: {escape(review.get("authority_reason", "не оценивалась"))}</p>'
                        f'<p>Материал: {escape(review.get("material_kind", "недоступен"))}. '
                        f'Группа происхождения: {escape(review.get("independence_group", "не установлена"))}</p></article>')
    facts = []
    for number, fact in enumerate(result.get("facts", []), 1):
        quotes = "".join(f'<blockquote>{escape(item["quote"])}</blockquote><p>{link(item["url"], "Сохранённый источник")} · '
                         f'{escape(item.get("semantic_status"))}: {escape(item.get("semantic_reason", ""))}</p>' for item in fact.get("evidence", []))
        facts.append(f'<article><h3>Факт {number}</h3><p>{escape(fact["text"])}</p>{quotes}</article>')
    support = result.get("core_support", {})
    support_lines = []
    for item in support.get("facts", []):
        groups = "".join('<li>' + " · ".join(link(url, url) for url in group) + '</li>' for group in item["groups"])
        support_lines.append(f'<p>Факт {item["fact_id"]}: независимых групп {len(item["groups"])}.</p><ul>{groups}</ul>')
    requests = journal.get("requests", [])
    rows = "".join(f'<tr><td>{index}</td><td>{escape(item.get("operation"))}</td><td>{escape(item.get("model", item.get("configured_model")))}</td>'
                   f'<td>{escape(item.get("status"))}</td><td>{escape((item.get("usage") or {}).get("input_tokens", "не сообщены"))}</td>'
                   f'<td>{escape((item.get("usage") or {}).get("output_tokens", "не сообщены"))}</td><td>{escape(item.get("error", ""))}</td></tr>'
                   for index, item in enumerate(requests, 1))
    totals = {name: sum((item.get("usage") or {}).get(name, 0) for item in requests) for name in ("input_tokens", "output_tokens", "total_tokens")}
    warnings = "".join(f'<li>{escape(note)}</li>' for note in result.get("warnings", []))
    blockers = "".join(f'<li>{escape(note)}</li>' for note in result.get("blocking_issues", []) + result.get("manual_review_reasons", []))
    rejected = "".join(f'<li>{escape(item.get("reason"))} — {link(item.get("url", ""), "цитата/источник")}</li>' for item in result.get("rejected_evidence", []))
    assessment = result.get("topic_assessment", {})
    return '''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Smartgram — исследование</title><style>body{max-width:1100px;margin:auto;padding:24px;background:#f5f6fa;color:#1e293b;font:17px/1.6 system-ui}
section,article{background:white;border:1px solid #ddd;border-radius:12px;padding:20px;margin:16px 0}a{color:#215e9f;overflow-wrap:anywhere}
blockquote{border-left:3px solid #789;padding-left:16px}table{border-collapse:collapse;font-size:14px;display:block;overflow:auto}td,th{border:1px solid #ccc;padding:8px;text-align:left}</style><main>''' + (
        f'<h1>{escape(result["topic"])}</h1><p>Режим: {escape(result.get("mode", "real"))}. Статус: <strong>{escape(result["status"])}</strong>.</p>'
        f'<p>{escape(result["explanation"])}</p><section><h2>Достаточно ли данных для сценария</h2>'
        f'<p>{escape(assessment.get("reason", "Не оценено"))}</p><p>Автоматическое продолжение: '
        f'{"разрешено" if result["status"] == "researched" else "запрещено"}.</p>'
        f'<h3>Предупреждения</h3><ul>{warnings}</ul><h3>Причины остановки / ручной проверки</h3><ul>{blockers}</ul></section>'
        f'<section><h2>Принятые и отклонённые источники ({len(sections)})</h2>{"".join(sections)}</section>'
        f'<section><h2>Факты и доказательства ({len(facts)})</h2>{"".join(facts) or "Подтверждённых фактов нет."}'
        f'<h3>Отклонённые доказательства</h3><ul>{rejected}</ul></section>'
        f'<section><h2>Основное объяснение и независимость</h2><p>{escape(support.get("reason", "Подтверждение ещё не установлено"))}</p>'
        + "".join(support_lines) + '</section>'
        f'<section><h2>Запросы и usage</h2><p>Попыток OpenAI: {len(requests)}. Раундов поиска: {len(result.get("search_rounds", []))}. '
        f'Уникальных загружавшихся страниц: {len(result.get("source_pages", []))}.</p>'
        f'<p>Токены, сообщённые API: вход {totals["input_tokens"]}, выход {totals["output_tokens"]}, всего {totals["total_tokens"]}.</p>'
        '<table><tr><th>№</th><th>Операция</th><th>Точная модель</th><th>Статус</th><th>Вход</th><th>Выход</th><th>Ошибка</th></tr>'
        f'{rows}</table></section><p>Оценка страниц и смысла цитат выполнена моделью и требует критического просмотра. '
        'Дословность проверена программой по сохранённым текстам. Это не проверка сценария. Сценарий, озвучка, картинки и монтаж не запускались в режиме только исследования.</p></main></html>')


def build_research_report(directory: Path) -> Path:
    result = json.loads((directory / "research.json").read_text(encoding="utf-8"))
    usage_path = directory / "research_usage.json"
    journal = json.loads(usage_path.read_text(encoding="utf-8")) if usage_path.exists() else {"requests": []}
    path = directory / "research_report.html"
    atomic_write(path, render_report(result, journal).encode("utf-8"))
    return path


def main() -> int:
    from html_report_sync import sync_generated_report

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-directory", type=Path, required=True)
    args = parser.parse_args()
    path = build_research_report(args.run_directory)
    print(path)
    return 0 if sync_generated_report(path) else 1


if __name__ == "__main__":
    raise SystemExit(main())
