"""Pure standalone HTML report builder. Deliberately no sync or billing refresh."""

import argparse
import base64
import html
import io
import json
from collections import Counter
from pathlib import Path
from PIL import Image, ImageOps
from production_state import inside, read


def escaped(value):
    return html.escape(str(value))



def source_author(source):
    author=escaped(source["author"])
    if source.get("origin")=="pexels":
        return '<a href="'+escaped(source["photographer_url"])+'">'+author+'</a> · <a href="https://www.pexels.com/">Pexels</a>'
    return author

def details(title, value):
    return (
        "<details><summary>"
        + escaped(title)
        + "</summary><pre>"
        + escaped(json.dumps(value, ensure_ascii=False, indent=2))
        + "</pre></details>"
    )


def picture(path):
    with Image.open(path) as src:
        image = ImageOps.exif_transpose(src).convert("RGB")
        image.thumbnail((300, 450))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=85)
    return (
        '<img alt="Сохранённый материал или извлечённый кадр" src="data:image/jpeg;base64,'
        + base64.b64encode(buffer.getvalue()).decode()
        + '">'
    )


def build_report(batch_directory, destination, smoke_directory=None):
    batch = Path(batch_directory)
    summary = read(batch / "batch_summary.json")
    if any(read(Path(topic['directory'])/'visual_materials.json').get('schema_version') == 2 for topic in summary['topics']):
        from shots_report import build_shots_report
        return build_shots_report(batch_directory, destination)
    sections = []
    for topic in summary["topics"]:
        root = Path(topic["directory"])
        script = read(root / "script.json")
        visuals = read(root / "visual_materials.json")
        render = (
            read(root / "render_manifest.json")
            if (root / "render_manifest.json").exists()
            else {
                "status": "not_started",
                "reason": "Монтаж в этом запуске не выполнялся.",
            }
        )
        state = (
            read(root / "visual_materials_state.json")
            if (root / "visual_materials_state.json").exists()
            else {"jobs": {}, "operations": {}}
        )
        usage = (
            read(root / "openai_usage.json")
            if (root / "openai_usage.json").exists()
            else {"requests": []}
        )
        body = f'<h2>{escaped(topic["topic"])}</h2><p>Версия <code>{escaped(topic["script_version_id"])}</code></p>'
        accepted = sum(
            entry.get("status") == "approved" for entry in visuals["fragments"]
        )
        body += f'<p>Принято {accepted} из {len(script["fragments"])} фрагментов.</p>'
        body += f'<p>Материалы: <b>{escaped(visuals["status"])}</b>. Монтаж: <b>{escaped(render["status"])}</b>.</p><p>{escaped(render.get("reason",""))}</p>'
        body += (
            "<p>Время этой команды: "
            + str(round(topic["duration_seconds"], 1))
            + " с. Подтверждённые расходы: неизвестны; суммы не получены из Costs API. Usage сохранён для последующего распределения.</p>"
        )
        for entry, fragment in zip(visuals["fragments"], script["fragments"]):
            body += f'<h3>Фрагмент {entry["fragment_index"]}: {escaped(entry["status"])}</h3><p>{escaped(fragment["text"])}</p><p><em>{escaped(fragment["visual"])}</em></p>'
            if entry.get("asset"):
                asset = entry["asset"]
                source = asset["source"]
                body += (
                    "<p>Выбранное изображение:</p>"
                    + picture(inside(root, asset["original_path"]))
                    + '<p>Превью движения: 0 / 0,5 / 1; секунды не назначены.</p><div class="frames">'
                    + "".join(
                        picture(inside(root, p["path"]))
                        for p in asset["motion"]["previews"]
                    )
                    + "</div>"
                )
                body += details(
                    "Решение ИИ и параметры подготовки",
                    {"review": entry["review"], "motion": asset["motion"]},
                )
                body += f'<p>{escaped(source["title"])} — {source_author(source)}. <a href="{escaped(source["source_page"])}">Источник</a>; <a href="{escaped(source["license_url"])}">{escaped(source["license"])}</a>. Изменения: {escaped(asset["changes"])}.</p>'
            else:
                body += "<p>" + escaped(entry.get("reason", "")) + "</p>"
                attempts = [
                    job
                    for job in state["jobs"].values()
                    if job["fragment_index"] == entry["fragment_index"]
                    and job.get("fragment") == fragment
                ]
                for job in attempts:
                    rounds = job["rounds"]
                    searches = sum(item["calls"] for item in rounds)
                    candidates = sum(len(item["candidates"]) for item in rounds)
                    reasons = Counter(item["reason"] for item in job["rejections"])
                    body += f"<p>Раундов: {len(rounds)}; поисковых обращений (включая неуспех): {searches}; технически пригодных кандидатов: {candidates}.</p>"
                    if reasons:
                        body += (
                            "<p>Причины отказов:</p><ul>"
                            + "".join(
                                "<li>" + escaped(reason) + " — " + str(count) + "</li>"
                                for reason, count in reasons.items()
                            )
                            + "</ul>"
                        )
                    elif searches:
                        body += "<p>Поиск не дал пригодных кандидатов. История запросов ниже; перегрузка сервиса отдельно указана в начале отчёта.</p>"
                body += "<p>Принятых превью нет. Подготовка и ИИ-проверка готового показа не подтверждены; изображения не подменялись.</p>"
        jobs = []
        for job in state["jobs"].values():
            jobs.append(
                {
                    "fragment_index": job["fragment_index"],
                    "status": job["status"],
                    "corrections": job["corrections"],
                    "rounds": [
                        {
                            "round": r["number"],
                            "calls": r["calls"],
                            "queries": r.get("queries"),
                            "candidates": r["candidates"],
                            "selection": r.get("selection"),
                        }
                        for r in job["rounds"]
                    ],
                    "rejections": job["rejections"],
                }
            )
        candidates = {
            c["candidate_id"]: c
            for job in state["jobs"].values()
            for rd in job["rounds"]
            for c in rd["candidates"]
        }
        downloaded = [c for c in candidates.values() if c.get("original_path")]
        if downloaded:
            body += '<h3>Скачанные кандидаты — это не принятые материалы</h3><div class="frames">'
            for candidate in downloaded:
                body += (
                    "<figure>"
                    + picture(inside(root, candidate["original_path"]))
                    + "<figcaption>"
                    + escaped(candidate["title"])
                    + " — "
                    + source_author(candidate)
                    + '. <a href="'
                    + escaped(candidate["source_page"])
                    + '">Источник</a>; <a href="'
                    + escaped(candidate["license_url"])
                    + '">'
                    + escaped(candidate["license"])
                    + "</a>.</figcaption></figure>"
                )
            body += "</div>"
        calls = [
            {
                "step": r.get("step"),
                "model": r.get("telemetry", {}).get("model"),
                "status": r.get("status", r.get("telemetry", {}).get("status")),
                "response_id": r.get("telemetry", {}).get("provider_response_id"),
                "duration_seconds": r.get("telemetry", {}).get("duration_seconds"),
                "usage": r.get("telemetry", {}).get("usage"),
                "error": r.get("telemetry", {}).get("error"),
            }
            for r in usage["requests"]
        ]
        body += (
            "<p>Фактические операции: "
            + escaped(
                {
                    step: sum(op["step"] == step for op in state["operations"].values())
                    for step in (
                        "visual_queries",
                        "visual_search",
                        "visual_download",
                        "visual_select",
                        "visual_review",
                    )
                }
            )
            + "</p>"
        )
        body += details(
            "Все попытки поиска, источники, лицензии, отклонения и бюджеты", jobs
        ) + details("Реальные вызовы OpenAI и usage; стоимость не распределена", calls)
        body += details("Статус и контракт монтажа", render)
        if (root / "subtitles.srt").exists():
            body += details("Субтитры", (root / "subtitles.srt").read_text())
        if (root / "video.mp4").exists() and render["status"] == "ready":
            body += (
                '<video controls src="data:video/mp4;base64,'
                + base64.b64encode((root / "video.mp4").read_bytes()).decode()
                + '"></video>'
            )
        sections.append("<section>" + body + "</section>")
    testing = ""
    if smoke_directory:
        smoke = Path(smoke_directory)
        test = read(smoke / "smoke_result.json")
        quality = read(smoke / "quality_report.json")
        video = (
            '<video controls src="data:video/mp4;base64,'
            + base64.b64encode((smoke / "test_video.mp4").read_bytes()).decode()
            + '"></video>'
        )
        testing = (
            "<section><h2>Отдельный синтетический тест FFmpeg</h2><p>Тестовый звук и рисунки; модельные ответы подставлены. Это не речь и не готовый ролик одной из тем. Настоящих API-вызовов: 0.</p>"
            + video
            + details("Результат теста", test)
            + details(
                "Декодирование, измерения AAC и фактическая шкала кадров",
                quality["technical"],
            )
            + "</section>"
        )
    service_notice = (
        "<p>Остановка после общей ошибки сервиса: "
        + escaped(summary["service_error"])
        + "</p>"
        if summary.get("service_error")
        else ""
    )
    document = (
        """<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="smartgram-report" content="isolated-visuals-editing"><title>Smartgram — материалы и монтаж</title><style>body{max-width:1120px;margin:40px auto;padding:0 20px;font:17px/1.55 system-ui;background:#f2f5f7;color:#13212d}section{background:white;border-radius:16px;padding:24px;margin:24px 0}h1,h2{line-height:1.2}code{overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}summary{cursor:pointer;font-weight:bold;padding:12px 0}img{max-width:30%;max-height:380px;object-fit:contain}video{max-width:100%;max-height:600px}.frames{display:flex;gap:10px}a{color:#075b8f}</style><h1>Smartgram: поиск и подготовка визуальных материалов</h1><p>Объединённый основной Smartgram. Исследователь, сценарист, TTS и распознавание аудио в этом визуальном запуске не вызывались. Изображения подбираются независимо от готовности голоса; финальный монтаж не запускался. Ни один заблокированный результат не объявлен готовым.</p><p>Конфигурация: снимок спецификации из задания; полный текст промптов сохранён. Шрифт явно выбран Arial 56 px при ширине 1080; он измеряется Pillow и проверяется фактическим выводом libass. Прочие заводские ограничения сохранены.</p>"""
        + service_notice
        + "".join(sections)
        + testing
        + "</html>"
    )
    # Same secret detector as the safe review archive; local-only exception does not relax secrecy.
    from build_review_archive import SECRET_VALUE
    from dotenv import dotenv_values
    from billing_api import local_secret_values

    secrets = local_secret_values(
        dotenv_values(Path(__file__).with_name(".env"), interpolate=False),
        include_environment=True,
    )
    if SECRET_VALUE.search(document) or any(secret in document for secret in secrets):
        raise ValueError("Possible secret in HTML; file not written.")
    destination = Path(destination)
    destination.write_text(document, encoding="utf-8")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--smoke", type=Path)
    args = parser.parse_args()
    print(build_report(args.batch, args.output, args.smoke))
