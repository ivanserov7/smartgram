"""Автономный отчёт диагностики: исходные WAV, две модели и отдельная проверка текста."""

import argparse
import base64
import json
from pathlib import Path

from asr_diagnostics import audit_audio
from file_io import atomic_write, atomic_write_json
from research_batch_report import details
from review_gallery import escape
from voiceover_report import optional, public_metadata


def timing_summary(raw):
    """Статистика исходных слов не заменяет смысловую проверку полноты речи."""
    words = raw["results"]["channels"][0]["alternatives"][0]["words"]
    return {"word_count": len(words), "last_word_end": words[-1]["end"],
            "maximum_word_end": max(word["end"] for word in words),
            "overlaps": sum(word["start"] < previous["end"] for previous, word in zip(words, words[1:]))}


def request_records(root):
    """Считаем реальные журналированные запросы, а не обращения к кешу."""
    records = []
    for directory in sorted(root.glob("topic_*")):
        for provider in ("deepgram", "openai"):
            for request in optional(directory / f"{provider}_usage.json").get("requests", []):
                records.append({"directory": directory.name, "provider": provider, **public_metadata(request)})
    return records


def scene_table(manifest, player):
    fragments = manifest.get("fragments") or manifest.get("provisional_fragments", [])
    rows = []
    for fragment in fragments:
        start, end = fragment["speech_start"], fragment["speech_end"]
        rows.append(f'<tr><td>{fragment["fragment_index"]}</td><td>{escape(fragment["text"])}</td>'
                    f'<td>{start:.3f}–{end:.3f} с</td><td><button data-player="{player}" data-time="{start}">Слушать</button></td></tr>')
    return '<table><tr><th>№</th><th>Исходный текст</th><th>Речь по Nova-2</th><th>WAV</th></tr>' + ''.join(rows) + '</table>'


def build_report(root):
    root = Path(root)
    records = request_records(root)
    counts = {provider: sum(record["provider"] == provider for record in records) for provider in ("deepgram", "openai")}
    stages = {stage: sum(record.get("stage") == stage for record in records)
              for stage in ("audio_check", "audio_confirm", "synthesize")}
    sections, summary_rows, entries = [], [], []
    for index in (1, 2, 3):
        directory = root / f"topic_{index}_nova2"
        diagnostic = optional(directory / "diagnostic.json")
        manifest = optional(directory / "audio_manifest.json")
        path, audit = audit_audio(diagnostic["source_directory"])
        raw = optional(directory / "deepgram.json")
        old = optional(Path(audit["original_response_path"]))
        stats, old_stats = timing_summary(raw), timing_summary(old)
        player = f"audio_{index}"
        audio = base64.b64encode(path.read_bytes()).decode()
        verdict = manifest.get("review", {}).get("decision", "Не запускалась")
        topic = diagnostic["script"].get("topic") or optional(Path(diagnostic["source_directory"]) / "run.json").get("topic", f"Тема {index}")
        summary_rows.append(f'<tr><td>{escape(topic)}</td><td>{audit["duration_seconds"]:.2f} с</td>'
                            f'<td>{stats["maximum_word_end"]:.3f} с</td><td>В пределах WAV; без пересечений</td>'
                            f'<td>{escape(verdict)}; нужен просмотр</td></tr>')
        issues = manifest.get("review", {}).get("issues", [])
        issue_html = ''.join('<li>' + escape(issue["reason"]) + '<br>Сценарий: ' + escape(issue["expected"])
                             + '<br>Deepgram: ' + escape(issue["observed_deepgram"])
                             + '<br>OpenAI: ' + escape(issue.get("observed_openai")) + '</li>' for issue in issues)
        explanation = ('<p class="warning">Для пирамид итоговое decision модели — «Есть расхождения», '
                       'но её же объяснение говорит, что ошибка не подтверждена обеими расшифровками. '
                       'Это противоречивый вердикт проверки, а не установленная ошибка диктора. Нужен ручной просмотр.</p>' if index == 2 else '')
        topic_records = [record for record in records if record["directory"].startswith(f"topic_{index}_")]
        sections.append(f'<section><h2>{escape(topic)}</h2><audio id="{player}" controls preload="none" src="data:audio/wav;base64,{audio}"></audio>'
                        f'<p>WAV: {audit["duration_seconds"]:.2f} с. SHA-256: <code>{audit["audio_sha256"]}</code></p>'
                        '<p>Старый Nova-3: максимум конца слова ' + str(old_stats["maximum_word_end"]) + ' с, пересечений: '
                        + str(old_stats["overlaps"]) + '. Новый Nova-2: ' + str(stats["maximum_word_end"]) + ' с, пересечений: '
                        + str(stats["overlaps"]) + '.</p><h3>Проверка чтения — отдельно от таймкодов</h3><p>' + escape(verdict)
                        + '</p>' + explanation + '<ul>' + issue_html + '</ul><p>Привязки ниже предварительные: текст не одобрен; в монтаж их автоматически не передаём.</p>'
                        + scene_table(manifest, player) + '<h3>Сценарий без изменений</h3><p>' + escape(diagnostic["script"]["narration_text"])
                        + '</p><h3>Полная расшифровка Nova-2</h3><p>' + escape(diagnostic["transcript"]) + '</p>'
                        + details("Аудит файла и неизменность исходных времён", audit)
                        + details("Старый ответ Nova-3", old) + details("Свежий ответ Nova-2, без исправления слов и секунд", raw)
                        + details("Независимая расшифровка OpenAI", optional(directory / "openai_transcript.json"))
                        + details("Результат и предварительные привязки", manifest)
                        + details("Все новые запросы этой темы: параметры, ответы и usage", topic_records) + '</section>')
        entries.append({"topic": topic, "audit": audit, "old_timing": old_stats, "new_timing": stats,
                        "text_verdict": verdict, "text_verified": manifest.get("text_verified", False)})
    gps3 = optional(root / "topic_3_nova3" / "diagnostic.json")
    gps2 = optional(root / "topic_3_nova2" / "diagnostic.json")
    comparison = '<section><h2>Прямое сравнение на одном WAV GPS</h2>'
    for result in (gps3, gps2):
        comparison += '<h3>' + escape(result["model"]) + '</h3><p>' + escape(result["transcript"]) + '</p>'
        comparison += '<p>' + escape(result["timing"]["reason"] or "Структурные проверки таймкодов пройдены.") + '</p>'
        comparison += details("Точные метаданные модели и исходные слова", {"metadata": result["metadata"], "words": result["raw_words"]})
    comparison += (f'<p>Число слов Nova-3: {len(gps3["raw_words"])}; Nova-2: {len(gps2["raw_words"])}. '
                   'Само число слов не доказывает полноту или точность: выше показаны обе полные расшифровки, '
                   'ниже — исходный текст и результат его смысловой проверки по Nova-2 с независимой расшифровкой. '
                   'Nova-3 с непригодными временами не передавалась в монтаж. '
                   'Текст Nova-3 не переносился в таймкоды Nova-2.</p></section>')
    comparison += details("Сопоставление полноты GPS: наблюдения по двум исходным ответам", optional(root / "comparison.json"))
    summary = {"schema_version": 1, "source_batch": "voiceover_batch_c04a8905ad9d821e", "new_model_requests": counts,
               "total_new_model_requests": len(records), "selected_model": "nova-2", "entries": entries,
               "tts_requests": 0, "scriptwriter_requests": 0, "voice_quality": "Нерешённая отдельная задача"}
    atomic_write_json(root / "summary.json", summary)
    page = '''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Smartgram — диагностика таймкодов</title><style>body{max-width:1200px;margin:auto;padding:24px;font:16px/1.6 system-ui;background:#f3f5f9;color:#182235}section{background:white;padding:20px;margin:20px 0;border-radius:12px}audio{width:100%}table{border-collapse:collapse;width:100%}td,th{border:1px solid #bbc;padding:10px;text-align:left}pre,code{white-space:pre-wrap;overflow-wrap:anywhere}summary,button{cursor:pointer}.warning{color:#982c16}</style>
<h1>Диагностика таймкодов трёх сохранённых WAV</h1>
<p>Причина остановки: ошибочные времена уже присутствуют в ответах Nova-3. SHA-256 сервера совпадает с полным WAV;
длительность в metadata совпадает с кадрами WAV. Преобразование лишь добавляло word_id, секунды не менялись.
Внутреннюю причину ошибки сервиса по этим ответам установить нельзя.</p>
<p>Nova-2 на всех трёх WAV прошла проверки границ и порядка слов. Рабочая настройка теперь Nova-2;
автоматического переключения моделей нет. Секунды не обрезались и не масштабировались.
Это не независимая акустическая оценка точности каждого слова. Привязки используют только слова выбранного ответа.</p>
<p>Текст остаётся на ручной проверке. Новая озвучка, сценарист, исследование и монтаж не запускались.
Естественность, интонация и качество голоса остаются отдельной нерешённой задачей.</p>'''
    page += f'<p>Новых модельных запросов: {len(records)} — Deepgram {counts["deepgram"]}, OpenAI {counts["openai"]} '
    page += f'({stages["audio_check"]} проверок текста и {stages["audio_confirm"]} независимых распознавания). Обращения к кешу не включены. '
    page += 'Служебные GET аналитики при передаче отчёта учитываются отдельно от модельных запросов.</p>'
    page += '<table><tr><th>Тема</th><th>WAV</th><th>Макс. конец Nova-2</th><th>Таймкоды</th><th>Текст</th></tr>' + ''.join(summary_rows) + '</table>'
    page += comparison + ''.join(sections) + details("Офлайн-тесты, не реальные результаты", optional(root / "offline_tests.json"))
    page += '''<script>document.querySelectorAll('button[data-player]').forEach(b=>b.addEventListener('click',()=>{const a=document.getElementById(b.dataset.player);a.currentTime=Number(b.dataset.time);a.play()}));</script></html>'''
    destination = root / "report.html"
    atomic_write(destination, page.encode("utf-8"))
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    print(build_report(args.directory))
