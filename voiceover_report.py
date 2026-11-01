"""Автономный HTML с полными WAV и таймкодами; не проверка интонации человеком."""

import base64
import json
from pathlib import Path

from file_io import atomic_write
from research_batch_report import details
from review_gallery import escape
from voiceover import audio_result


def optional(path):
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except (ValueError, OSError):
        return {}


def public_metadata(value):
    """Локальные отпечатки ключей нужны биллингу, а не HTML для ревью."""
    if isinstance(value, dict):
        return {name: public_metadata(item) for name, item in value.items()
                if name.casefold() not in {"api_key_fingerprint", "key_fingerprints", "authorization"}}
    if isinstance(value, list):
        return [public_metadata(item) for item in value]
    return value


def build_voiceover_report(batch, destination):
    summaries, sections = [], []
    for index, entry in enumerate(batch["entries"], 1):
        directory = Path(entry.get("directory") or destination.parent / f"not_started_{index}")
        manifest = optional(directory / "audio_manifest.json")
        state = optional(directory / "voiceover_state.json")
        script_state = optional(directory / "script_state.json")
        operations = list(state.get("operations", {}).values())
        counts = {stage: sum(op["stage"] == stage for op in operations) for stage in ("synthesize", "audio_check", "audio_confirm")}
        duration = manifest.get("duration_seconds")
        target = manifest.get("target_duration_seconds") or {"min": 30, "max": 45}
        status = manifest.get("status") or entry.get("status", "Не запускалась")
        reason = manifest.get("reason") or entry.get("error")
        player = '<p>Аудио не создано.</p>'
        if manifest.get("audio_path"):
            try:
                path, measured = audio_result(directory, manifest)
                encoded = base64.b64encode(path.read_bytes()).decode("ascii")
                uri = "data:audio/wav;base64," + encoded
                player = (f'<audio id="audio-{index}" controls preload="none" src="{uri}"></audio>'
                          f'<p><a download="Smartgram_voice_{index}.wav" href="{uri}">Скачать WAV</a></p>'
                          + ((('<p class="warning">Текст принят OpenAI; остаётся отдельная проблема технической проверки или таймкодов.</p>' if manifest.get('text_verified') else '<p class="warning">Аудио отклонено или ещё не проверено. Наличие плеера не означает одобрения.</p>') if status != 'completed' else '')))
            except Exception:
                player = '<p class="warning">WAV повреждён/изменён; плеер не подставлялся.</p>'
        rows = []
        for fragment in manifest.get("fragments") or manifest.get("provisional_fragments", []):
            start, end = fragment["speech_start"], fragment["speech_end"]
            rows.append(f'<tr><td>{fragment["fragment_index"]}</td><td>{escape(fragment["text"])}</td><td>{escape(fragment["visual"])}</td>'
                        f'<td><button data-player="audio-{index}" data-time="{start}">{start:.3f} с</button></td><td>{end:.3f} с</td></tr>')
        if not rows:
            versions = script_state.get("versions", [])
            source_fragments = versions[-1]["script"]["fragments"] if versions else entry.get("source_fragments", [])
            rows = [f'<tr><td>{number}</td><td>{escape(fragment["text"])}</td><td>{escape(fragment["visual"])}</td>'
                    '<td>Не подтверждено</td><td>Не подтверждено</td></tr>' for number, fragment in enumerate(source_fragments, 1)]
        usage = {name: optional(directory / name) for name in ("openai_usage.json", "gemini_usage.json", "deepgram_usage.json", "script_usage.json")}
        config = manifest.get("settings_snapshot", {}).get("settings", {})
        provider = manifest.get("provider") or config.get("tts_provider", "—")
        model = manifest.get("model") or config.get("tts_model", "—")
        voice = manifest.get("voice") or config.get("tts_voice", "—")
        style = manifest.get("style") or config.get("tts_style", "—")
        listening='Пользователь подтвердил, что сохранённая дорожка его устраивает.' if manifest.get('listener_feedback',{}).get('status')=='approved_by_user' else 'Ожидает прослушивания пользователя.'
        visible_settings = (f'<p><strong>Синтез:</strong> {escape(provider)} · {escape(model)} · {escape(voice)}</p>'
            f'<p><strong>Стиль:</strong> {escape(style)}</p><p><strong>Качество русского звучания:</strong> {escape(listening)}</p>'
            '<p>Четыре шага: 1. Синтез → 2. Две расшифровки → 3. Проверка текста и привязка → 4. Результат.</p>')
        if manifest.get("provisional_fragments") and not manifest.get("fragments"):
            visible_settings += ('<p class="warning">Текст принят; интервалы ниже требуют отдельной проверки.</p>' if manifest.get('text_verified') else '<p class="warning">Интервалы ниже предварительные; текст не одобрен для автоматического монтажа.</p>')
        summaries.append(f'<tr><td><a href="#topic-{index}">{escape(entry["topic"])}</a></td><td>{escape(status)}</td><td>{escape(duration)}</td>'
                         f'<td>{target["min"]}–{target["max"]} с</td><td>{escape((manifest.get("script_version_id") or "—")[:12])}</td>'
                         f'<td>{counts["synthesize"]} ({sum(state.get("regenerations_by_version", {}).values())})</td><td>{counts["audio_check"]} / {counts["audio_confirm"]}</td>'
                         f'<td>{script_state.get("revisions_used", "—")}</td></tr>')
        diagnostics = []
        for path in sorted((directory / "voiceover").glob("*/*.json")):
            diagnostics.append(details(str(path.relative_to(directory)), optional(path)))
        sections.append(f'<section id="topic-{index}"><h2>{escape(entry["topic"])}</h2><p>Статус: {escape(status)}. '
                        f'Причина остановки: {escape(reason or "Нет")}</p><p>Голос создан ИИ. Длительность измерена по кадрам WAV: {escape(duration)} с.</p>'
                        + visible_settings + player + f'<h3>Полный текст для озвучки</h3><pre>{escape(manifest.get("narration_text") or "Аудио не создано.")}</pre>'
                        f'<h3>Расшифровка Deepgram</h3><p>{escape(manifest.get("transcript") or "Не получена")}</p>'
                        f'<h3>Независимая расшифровка OpenAI</h3><p>{escape(manifest.get("openai_transcript", {}).get("text") or "Не получена")}</p>'
                        + details("Подтверждение текста по OpenAI", manifest.get('text_confirmation'))
                        + details("Независимая техническая проверка текущего WAV", manifest.get('technical_checks'))
                        + details("Текущий итог проверки обеих расшифровок и привязки", manifest.get("review"))
                        +
                        '<div class="table"><table><tr><th>Фрагмент</th><th>Текст</th><th>Что показать</th><th>Начало речи</th><th>Конец речи</th></tr>'
                        + ''.join(rows) + '</table></div>' + details("Происхождение и исходные fragments", entry)
                        + details("Итоговый манифест; границы речи и показа отдельно", manifest)
                        + details("Все попытки, операции и причины остановки", state)
                        + details("История сценариста и общий бюджет доработок", script_state)
                        + details("Запросы Gemini, OpenAI и Deepgram; usage и оценки раздельно", public_metadata(usage))
                        + ''.join(diagnostics) + '</section>')
    experiment_notes = ''.join(f'<p>{escape(note)}</p>' for note in batch.get("comparison_notes", []))
    page = '''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Smartgram — полная озвучка</title><style>body{max-width:1250px;margin:auto;padding:24px;background:#f4f6f9;color:#1e293b;font:16px/1.6 system-ui}section,.notice{background:white;padding:22px;margin:22px 0;border-radius:12px}audio{width:100%}.table{overflow:auto}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccd;padding:9px;text-align:left;vertical-align:top}pre{white-space:pre-wrap;overflow-wrap:anywhere}summary,button{cursor:pointer}.warning{color:#a32424}</style><main><h1>Smartgram · озвучка трёх тем</h1>
<div class="notice">Голос создан ИИ. Проверка текста предназначена для сопоставления расшифровок и не оценивает ударения, естественность и интонацию.
Все доступные WAV встроены в HTML. Deepgram — отдельный поставщик; его стоимость неизвестна («—»), не 0.
Usage OpenAI не является точным списанием: фактические суммы — в отдельной аналитике Costs API. Изображения и монтаж в этих прогонах не запускались.
Офлайн-тесты с подставными ответами не являются настоящими прогонами. Фактический исход каждой темы указан ниже.</div>''' + experiment_notes + '''
<div class="table"><table><tr><th>Тема</th><th>Статус</th><th>Длительность, с</th><th>Цель</th><th>Версия сценария</th><th>Генерации (повторные)</th><th>Проверки / независимый ASR</th><th>Доработки сценария, всего</th></tr>''' + ''.join(summaries) + '</table></div>' + details("Расходы по провайдерам отдельно: оценки и неизвестные суммы", batch.get("costs")) + details("Офлайн-проверки (не реальные прогоны)", batch.get("offline_validation")) + ''.join(sections) + '''</main><script>
document.querySelectorAll('button[data-player]').forEach(button=>button.addEventListener('click',()=>{const audio=document.getElementById(button.dataset.player);if(audio){audio.currentTime=Number(button.dataset.time);audio.play();}}));
</script></html>'''
    atomic_write(destination, page.encode("utf-8"))
    return destination
