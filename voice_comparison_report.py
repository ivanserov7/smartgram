"""Автономное слепое сравнение. Оценки остаются в браузере; победителя выбирает слушатель."""

import base64
import json
from pathlib import Path

from file_io import atomic_write
from research_batch_report import details
from review_gallery import escape
from voiceover_report import public_metadata


def player(path, label):
    if not path.is_file():
        return ""
    encoded = base64.b64encode(path.read_bytes()).decode()
    return (
        "<p>"
        + escape(label)
        + '</p><audio controls preload="none" src="data:audio/wav;base64,'
        + encoded
        + '"></audio>'
    )


def build_report(root, state, text):
    root = Path(root)
    entries = {entry["id"]: entry for entry in state["entries"]}
    cards = []
    for identifier in state["blind_order"]:
        entry = entries[identifier]
        audio = entry.get("audio")
        directory = root / identifier
        media = (
            player(directory / "original.wav", "Исходное аудио")
            if audio
            else "<p>У этого образца пока нет успешно завершённого аудио.</p>"
        )
        if audio:
            media += player(
                directory / "level_matched.wav",
                "Копия с общей громкостью; линейное усиление",
            )
        review = entry.get("review") or {"status": "Проверка текста не завершена"}
        body = (
            '<article data-label="'
            + escape(entry["blind_label"])
            + '"><h2>'
            + escape(entry["blind_label"])
            + "</h2><p>Аудио сгенерировано ИИ.</p>"
            + media
        )
        body += (
            '<div class="sensitive"><h3>'
            + escape(entry["model"])
            + " / "
            + escape(entry["voice"])
            + "</h3>"
        )
        body += (
            "<p>Состояние: "
            + escape(entry["status"])
            + ". "
            + escape(entry.get("error") or "")
            + "</p>"
        )
        if audio:
            body += f'<p>Длительность: {audio["duration_seconds"]:.3f} с; частота: {audio["sample_rate"]} Гц. '
            body += f'Исходный RMS: {audio["rms_dbfs"]:.2f} dBFS; peak: {audio["peak_dbfs"]:.2f} dBFS.</p>'
        generation = state["operations"].get("generation:" + identifier, {})
        attempts = generation.get("attempts", [])
        known_prices = [
            a["estimated_usd"] for a in attempts if a.get("estimated_usd") is not None
        ]
        unknown_prices = [a for a in attempts if a.get("estimated_usd") is None]
        if known_prices:
            body += f"<p>Оценка генерации: ${sum(known_prices):.6f} (по тарифу/usage, не списание).</p>"
        else:
            body += "<p>Оценка генерации: не определена по данным ответа.</p>"
        if unknown_prices:
            body += f'<p>Неизвестных попыток: {len(unknown_prices)}; защитный резерв ${sum(a["reserved_usd"] for a in unknown_prices):.6f}. Резерв не является ценой.</p>'
        body += "<p>Подтверждённое списание этого образца: пока не установлено.</p>"
        if entry["api"] != "speech" and entry.get("generation"):
            body += (
                "<p>Модель в ответе API: "
                + escape(entry["generation"].get("model"))
                + "</p>"
            )
        body += (
            "<p>Полнота текста: "
            + escape(review["status"])
            + ". Это проверка расшифровки, не оценка акцента или интонации.</p>"
        )
        if entry["model"] == "tts-1-hd":
            body += "<p>Модель не поддерживает instructions: управляющий промпт ей не передавался.</p>"
        if entry["api"] == "speech":
            body += "<p>Модель указана в запросе; бинарный Speech API не вернул отдельное имя снимка и usage. Отсутствующие значения не восстановлены догадкой.</p>"
        if entry["model"] in ("tts-1-hd", "gpt-4o-mini-tts"):
            body += "<p>Исторический контроль: объявлено отключение 6 января 2027 года. Производственная модель не меняется.</p>"
        body += details(
            "Модель, голос, фактический запрос, длительность, исходная громкость и усиление",
            entry,
        )
        requests = []
        for path in sorted(directory.rglob("openai_usage.json")):
            requests += json.loads(path.read_text()).get("requests", [])
        body += (
            details(
                "Фактические параметры, ответы API и usage (без секретов)",
                public_metadata(requests),
            )
            + "</div>"
        )
        body += details("Исходный текст, одинаковый для всех", text)
        body += "<fieldset><legend>Ваша оценка: 1 — плохо, 5 — хорошо</legend>"
        for name, label in (
            ("accent", "Отсутствие иностранного акцента"),
            ("stress", "Ударения"),
            ("naturalness", "Естественность"),
            ("accuracy", "Точность чтения"),
            ("preference", "Общее предпочтение"),
        ):
            body += (
                "<label>"
                + label
                + ' <select data-score="'
                + name
                + '"><option value="">—</option>'
                + "".join(f"<option>{score}</option>" for score in range(1, 6))
                + "</select></label>"
            )
        body += '<label>Заметки <textarea data-score="notes"></textarea></label></fieldset></article>'
        cards.append(body)
    operations = list(state["operations"].values())
    counts = {
        stage: sum(len(op["attempts"]) for op in operations if op["stage"] == stage)
        for stage in ("generation", "transcription", "text_check")
    }
    known = sum(
        attempt["estimated_usd"]
        for op in operations
        for attempt in op["attempts"]
        if attempt.get("estimated_usd") is not None
    )
    unknown = sum(
        attempt.get("estimated_usd") is None
        for op in operations
        for attempt in op["attempts"]
    )
    generated = sum(bool(entry.get("audio")) for entry in state["entries"])
    page = """<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Smartgram — сравнение русской озвучки</title><style>body{max-width:1100px;margin:auto;padding:24px;background:#f3f5f8;color:#1b2538;font:16px/1.6 system-ui}article,.panel{background:white;border-radius:14px;padding:22px;margin:20px 0}audio{width:100%}pre{white-space:pre-wrap;overflow-wrap:anywhere}.sensitive{display:none}body.revealed .sensitive{display:block}label{display:block;margin:10px 0}textarea{width:95%;min-height:70px}summary,button,select{cursor:pointer}fieldset{border:1px solid #ccd;border-radius:8px}button{padding:9px 15px}</style>
<h1>Сравнение русской озвучки</h1><div class="panel"><p>Один и тот же текст GPS, отдельные запросы без истории других образцов. Все аудио созданы ИИ.
Сначала сравните записи на слух, затем раскройте модели. Для сопоставления используйте копии с общим RMS; оригиналы также сохранены. Цена и название модели не доказывают качество произношения.
Оценки сохраняются только в этом браузере; кнопка позволяет скачать их. Автоматического победителя нет.</p>
<label><input type="checkbox" id="reveal"> Показать модели, статусы текстовой проверки и расходы</label>
<button id="export">Скачать мои оценки JSON</button></div>"""
    page += (
        '<div class="panel sensitive"><h2>Итог и расходы</h2><p>Создано '
        + str(generated)
        + " из 8 образцов. Запросы: "
        + escape(counts)
        + ".</p>"
    )
    page += f"<p>Известный подытог оценок по usage/тарифу: ${known:.6f}. Попыток без точной оценки: {unknown}. "
    page += f'Консервативно занято бюджета (оценки плюс неосвобождённые резервы): ${state.get("budget_committed_usd",0):.6f} из $2. '
    page += (
        "Резерв не является расходом. Фактическое списание эксперимента: "
        + escape(state.get("confirmed_experiment_cost_usd") if state.get("confirmed_experiment_cost_usd") is not None else "пока не установлено")
        + ". "
        + escape(state.get("confirmed_cost_reason"))
        + "</p>"
    )
    page += details("Предварительная оценка, допущения и лимиты", state["preflight"])
    page += details(
        "Операции, попытки и раздельные оценки стоимости", state["operations"]
    )
    page += details(
        "Офлайн-проверки, не настоящие аудиопробы", state.get("offline_tests")
    )
    page += details(
        "Проверка Costs API; агрегат не является расходом этого эксперимента",
        state.get("billing_confirmation"),
    )
    page += '<p>Официальные источники: <a href="https://developers.openai.com/api/docs/pricing">тарифы</a>, <a href="https://developers.openai.com/api/docs/guides/text-to-speech">Speech</a>, <a href="https://developers.openai.com/api/docs/guides/realtime-conversations">Realtime</a>, <a href="https://developers.openai.com/api/docs/guides/live-conversations">Live</a>, <a href="https://developers.openai.com/api/docs/guides/audio-chat-completions">Chat audio</a>.</p></div>'
    page += "".join(cards)
    storage_key = (
        "SmartgramVoiceComparison:"
        + root.name
        + ":"
        + state["signature"]["text_sha256"]
    )
    page += (
        "<script>const storageKey="
        + json.dumps(storage_key)
        + """;let saved={};try{saved=JSON.parse(localStorage.getItem(storageKey)||'{}')}catch(e){}
document.getElementById('reveal').addEventListener('change',e=>document.body.classList.toggle('revealed',e.target.checked));
document.querySelectorAll('article').forEach(card=>{const label=card.dataset.label;card.querySelectorAll('[data-score]').forEach(input=>{input.value=(saved[label]||{})[input.dataset.score]||'';input.addEventListener('change',()=>{saved[label]=saved[label]||{};saved[label][input.dataset.score]=input.value;try{localStorage.setItem(storageKey,JSON.stringify(saved))}catch(e){}})})});
document.getElementById('export').addEventListener('click',()=>{const blob=new Blob([JSON.stringify({experiment:storageKey,ratings:saved},null,2)],{type:'application/json'});const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='Smartgram_voice_ratings.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)});
document.querySelectorAll('audio').forEach(player=>player.addEventListener('play',()=>document.querySelectorAll('audio').forEach(other=>{if(other!==player)other.pause()})));
</script></html>"""
    )
    atomic_write(root / "report.html", page.encode())
    return root / "report.html"
