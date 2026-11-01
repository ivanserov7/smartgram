"""Автономная галерея фактических кадров, проверок и всех ограниченных попыток."""

import base64
from collections import Counter
import io
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps
from file_io import atomic_write
from production_state import read, inside, digest
from production_report import details, escaped


def picture(path, maximum=(1800, 2400)):
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail(maximum)
        image.info.clear()
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=90)
    uri = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()
    return (
        '<a href="'
        + uri
        + '" target="_blank"><img src="'
        + uri
        + '" alt="Фактически сохранённый материал"></a>'
    )


def material_counts(state, job, job_key):
    found, viewed = set(), set()
    for operation in state["operations"].values():
        identity = operation.get("identity", {})
        if (
            operation["step"] == "commons_get"
            and identity.get("job") == job_key
            and identity.get("kind") == "search"
        ):
            for page in (
                (operation.get("result") or {}).get("query", {}).get("pages", [])
            ):
                if page.get("title", "").startswith("File:"):
                    found.add(page["title"])
        elif operation["step"] == "visual_select" and identity.get("data", {}).get(
            "shot", {}
        ).get("fragment_index") == job.get("fragment_index"):
            viewed.update(
                c["candidate_id"] for c in identity["data"].get("candidates", [])
            )
    return {
        "found": len(found),
        "rejected": len({r["candidate_id"] for r in job.get("rejections", [])}),
        "downloaded": len(job.get("assets_cache", {})),
        "viewed": len(viewed),
        "new_search_calls": sum(r["calls"] for r in job.get("rounds", [])),
        "retry_calls": sum(r.get("retry_calls", 0) for r in job.get("rounds", [])),
    }


def build_shots_report(batch_directory, destination):
    root = Path(batch_directory)
    batch = read(root / "batch_summary.json")
    sections = []
    totals = Counter()
    for topic in batch["topics"]:
        directory = Path(topic["directory"])
        script = read(directory / "script.json")
        manifest = read(directory / "visual_materials.json")
        state = (
            read(directory / "visual_materials_state.json")
            if (directory / "visual_materials_state.json").exists()
            else {"jobs": {}, "operations": {}}
        )
        entries = manifest["fragments"]
        shots = [s for f in entries for s in f.get("shots", [])]
        accepted = sum(s["status"] == "approved" for s in shots)
        closed = sum(f["status"] == "approved" for f in entries)
        totals["accepted"] += accepted
        totals["shots"] += len(shots)
        body = f'<h2>{escaped(topic["topic"])}</h2><p>Принято кадров: <b>{accepted}/{len(shots)}</b>; закрыто исходных фрагментов: <b>{closed}/{len(script["fragments"])}</b>.</p>'
        body += f'<p>Статус: {escaped(manifest["status"])}. Версия сценария: <code>{escaped(script["script_version_id"])}</code>; SHA-256: {escaped(topic["script_sha256"])}.</p>'
        body += f'<p>Источник: {escaped(topic["source_directory"])}. Время команд: {topic["duration_seconds"]:.1f} с. Времена кадров: ожидают принятых слов аудио. Озвучка и монтаж не запускались.</p>'
        for fragment, entry in zip(script["fragments"], entries):
            body += (
                "<article><h3>Исходный фрагмент "
                + str(entry["fragment_index"])
                + "</h3><p>"
                + escaped(fragment["text"])
                + "</p><p>Замысел: "
                + escaped(fragment["visual"])
                + "</p>"
            )
            body += (
                "<p>Состояние фрагмента: "
                + escaped(entry["status"])
                + ". "
                + escaped(entry.get("reason") or "")
                + "</p>"
            )
            children = entry.get("shots", [])
            if not children:
                body += (
                    "<p>План кадров ещё не создан. "
                    + escaped(entry.get("reason", ""))
                    + "</p>"
                )
            for shot in children:
                body += (
                    "<h4>"
                    + escaped(shot["shot_id"])
                    + " · "
                    + escaped(shot["status"])
                    + "</h4><p>"
                    + escaped(shot["text"])
                    + "</p><p>Задача кадра: "
                    + escaped(shot["visual_task"])
                    + "</p>"
                )
                body += f'<p>Точный диапазон символов [{shot["text_start"]}, {shot["text_end"]}); секунды не назначены.</p>'
                if shot.get("asset"):
                    asset = shot["asset"]
                    source = asset["source"]
                    body += (
                        '<div class="selected">'
                        + picture(inside(directory, asset["original_path"]))
                        + '</div><p>Начало / середина / конец движения (0 / 0.5 / 1):</p><div class="previews">'
                        + "".join(
                            picture(inside(directory, p["path"]))
                            for p in asset["motion"]["previews"]
                        )
                        + "</div>"
                    )
                    body += (
                        "<p>"
                        + escaped(source["title"])
                        + " — "
                        + escaped(source.get("author") or "Автор не указан источником")
                        + '. <a href="'
                        + escaped(source["source_page"])
                        + '">Страница произведения</a>. '
                    )
                    rights = source.get("rights_evidence", {})
                    if source.get("license_url"):
                        body += (
                            '<a href="'
                            + escaped(source["license_url"])
                            + '">'
                            + escaped(source["license"])
                            + "</a>"
                        )
                    else:
                        body += (
                            escaped(source["license"])
                            + "; основание: "
                            + escaped(rights.get("basis", ""))
                        )
                    body += (
                        "</p><p>Выбор ИИ: "
                        + escaped(shot.get("selection_reason", ""))
                        + "</p>"
                        + details(
                            "Проверка фактических превью и геометрия",
                            {
                                "review": shot["review"],
                                "motion": asset["motion"],
                                "rights": rights,
                                "source": source,
                            },
                        )
                    )
                else:
                    body += (
                        '<p class="warning">'
                        + escaped(shot.get("reason", "Подбор не завершён."))
                        + "</p>"
                    )
            key = digest({"fragment": fragment, "index": entry["fragment_index"]})
            job = state["jobs"].get(key, {})
            rounds = job.get("rounds", [])
            stats = material_counts(state, job, key)
            body += f'<p>Уникальных файлов: найдено {stats["found"]}, отклонено {stats["rejected"]}, скачано {stats["downloaded"]}, рассмотрено моделью {stats["viewed"]}. Принято кадров: {sum(child["status"]=="approved" for child in children)}. Поиск: {stats["new_search_calls"]} новых GET, {stats["retry_calls"]} повторов.</p>'
            unresolved = {
                child["shot_id"] for child in children if child["status"] != "approved"
            }
            for reason in [
                item
                for item in job.get("rejections", [])
                if item.get("shot_id") in unresolved
            ][-3:]:
                body += '<p class="warning">' + escaped(reason["reason"]) + "</p>"
            selected_ids = {
                child["asset"]["candidate_id"]
                for child in children
                if child.get("asset")
            }
            unused = [
                candidate
                for candidate in job.get("assets_cache", {}).values()
                if candidate["candidate_id"] not in selected_ids
            ]
            if unused:
                body += "<details><summary>Скачанные кандидаты, не принятые для этого фрагмента</summary>"
                for candidate in unused:
                    body += (
                        picture(inside(directory, candidate["original_path"]), maximum=(600, 900))
                        + "<p>ID "
                        + escaped(candidate["candidate_id"])
                        + ": "
                        + escaped(candidate["title"])
                        + " — "
                        + escaped(candidate.get("author") or "Автор не указан")
                        + '. <a href="'
                        + escaped(candidate["source_page"])
                        + '">Commons</a>; '
                        + escaped(candidate["license"])
                        + ".</p>"
                    )
                body += "</details>"
            body += (
                details("Поисковые запросы, все отказы и оставшийся бюджет", job)
                + "</article>"
            )
        usage = (
            read(directory / "openai_usage.json")
            if (directory / "openai_usage.json").exists()
            else {"requests": []}
        )
        calls = []
        for record in usage["requests"]:
            t = record["telemetry"]
            calls.append(
                {
                    "step": record["step"],
                    "model": t.get("model"),
                    "usage": t.get("usage"),
                    "status": t.get("status"),
                    "duration_seconds": t.get("duration_seconds"),
                    "response_id": t.get("provider_response_id"),
                    "prompt_sha256": hashlib.sha256(
                        record.get("instructions", "").encode()
                    ).hexdigest(),
                    "images": record.get("images"),
                    "request": record.get("request"),
                }
            )
        ops = [
            {k: v for k, v in op.items() if k != "result"}
            for op in state["operations"].values()
            if op["step"] == "commons_get"
        ]
        model_outcomes = {
            key: {name: op.get(name) for name in ("step", "status", "result", "reason")}
            for key, op in state["operations"].items()
            if op["step"] != "commons_get"
        }
        body += details(
            "Все структурированные ответы модели, включая первоначальные ошибки JSON",
            model_outcomes,
        )
        totals["model_calls"] += len(calls)
        totals["http_attempts"] += sum(len(o["attempts"]) for o in ops)
        body += details("HTTP GET: параметры, попытки, паузы и причины", ops) + details(
            "Реальные вызовы модели, usage и версии промптов", calls
        )
        sections.append("<section>" + body + "</section>")
    warning = ""
    if batch.get("service_error"):
        warning = (
            '<p class="warning">'
            + escaped(batch["service_error"])
            + "; retry_at="
            + escaped(batch.get("retry_at"))
            + "</p>"
        )
    document = """<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Smartgram — кадры и реальные материалы</title><style>body{max-width:1200px;margin:auto;padding:24px;font:16px/1.6 system-ui;background:#f3f5f7;color:#17202b}section,article{padding:20px;background:white;border-radius:12px;margin:20px 0;border:1px solid #dde2e9}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}img{max-width:100%;max-height:520px;object-fit:contain}.selected{max-width:500px}.previews{display:flex;gap:12px}.previews a{width:32%}summary{cursor:pointer;font-weight:bold}.warning{color:#a32424}code{overflow-wrap:anywhere}</style><h1>Smartgram: реальные изображения и подготовленные кадры</h1><p>Отдельный визуальный запуск по одобренным сценариям; без исследования, сценариста, TTS, ASR и финального монтажа. Принятие требует просмотра изображений и фактических превью моделью, не только метаданных.</p>"""
    document += (
        "<p>run_id: "
        + escaped(root.name)
        + "; предыдущий запуск: "
        + escaped(batch.get("previous_run", "—"))
        + "</p>"
        + warning
        + details("Итоговые счётчики", dict(totals))
    )
    document += (
        "<p>Подтверждённые расходы именно этого запуска неизвестны. Usage не равен списанию; аналитика Costs API обновляется штатным способом отдельно. Все показанные картинки и превью встроены в этот HTML.</p>"
        + "".join(sections)
        + "</html>"
    )
    atomic_write(Path(destination), document.encode())
    return Path(destination)
