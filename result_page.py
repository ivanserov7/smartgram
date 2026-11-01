"""Автономная страница результата запуска: сценарий, источники, сцены, картинки и WAV."""

import argparse
import base64
import json
import sys
from pathlib import Path

from audio_files import inspect_wav
from commons_api import VisualError
from file_io import atomic_write
from voice_errors import VoiceoverError
from review_gallery import credits, escape, link, local_image_data, render_candidate


def read_optional(directory: Path, name: str) -> dict:
    path = directory / name
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise VisualError(f"В {name} нужен объект JSON.")
    return value


def render_audio(directory: Path, audio: dict, scene: dict) -> tuple[str, float]:
    """Считает длительность по самим WAV; неверные и устаревшие файлы не выдаются за готовые."""
    if not audio or audio.get("narration") != scene["narration"]:
        return "<p>Озвучка этой сцены не создана.</p>", 0
    path = (directory / audio["audio_path"]).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise VisualError("Аудиофайл выходит за пределы папки запуска.")
    content = path.read_bytes()
    measured = inspect_wav(content)
    if measured["audio_sha256"] != audio["audio_sha256"]:
        raise VisualError("Хеш аудиофайла не соответствует манифесту.")
    duration = measured["duration_seconds"]
    url = "data:audio/wav;base64," + base64.b64encode(content).decode("ascii")
    return (f'<audio controls preload="none" src="{url}"></audio>'
            f'<p>Измеренная длительность: {duration:.3f} с · '
            f'{escape(audio.get("model"))} · голос {escape(audio.get("voice"))}</p>'), duration


def render_scene(scene: dict, entry: dict, audio: dict, directory: Path) -> tuple[str, float]:
    player, duration = render_audio(directory, audio, scene)
    chosen = entry.get("selected")
    picture = "<p>Проверенное изображение не выбрано.</p>"
    if chosen:
        path = chosen.get("analysis_image_path") or chosen["local_path"]
        picture = (f'<img class="chosen" src="{local_image_data(directory, path)}" alt="{escape(chosen["title"])}">'
                   f'<p>{escape(chosen["title"])}</p>{credits(chosen)}')
    previews = {item["file_title"]: {"status": "downloaded", "local_path": item["analysis_image_path"]}
                for item in entry.get("candidates", []) if item.get("analysis_image_path")}
    cards = "".join(render_candidate(item, directory, previews) for item in entry.get("candidates", []))
    return (f'<section><h2>Сцена {scene["id"]}</h2><p>{escape(scene["narration"])}</p>'
            f'<p class="meta">Визуальный план: {escape(scene["visual_description"])}</p>'
            f'<p>Запрос: {escape(entry.get("search_query", scene["search_query"]))}</p>{picture}'
            f'<p>{escape(entry.get("reason", "Визуальная проверка ещё не выполнена."))}</p>{player}'
            f'<details><summary>Кандидаты и объяснения ИИ</summary><div class="grid">{cards}</div></details></section>'), duration


def build_result_page(directory: Path, output: Path | None = None) -> Path:
    """Читает только локальные результаты; API не вызываются, отсутствующие этапы явно видны."""
    directory = directory.resolve()
    state = read_optional(directory, "run.json")
    research = read_optional(directory, "research.json")
    script = read_optional(directory, "script.json")
    script_state = read_optional(directory, "script_state.json")
    if script_state.get("schema_version") == 2 and script_state.get("versions"):
        # Последняя версия важнее старого script.json, одобренного до доработки.
        from scriptwriter import narration_text
        latest = script_state["versions"][-1]
        script = {**latest["script"], "schema_version": 2, "script_version_id": latest["script_version_id"],
                  "narration_text": narration_text(latest["script"])}
    if not script:
        versions = sorted(directory.glob("script_[1-3].json"))
        script = read_optional(directory, versions[-1].name) if versions else {}
    plan = read_optional(directory, "scenes.json")
    audio = read_optional(directory, "audio_manifest.json")
    visual = read_optional(directory, "visual_review.json")
    if script.get("schema_version") == 2:
        current_id = script["script_version_id"]
        if script_state.get("status") != "approved":
            plan, audio, visual = {}, {}, {}
        else:
            plan = plan if plan.get("script_version_id") == current_id else {}
            audio = audio if audio.get("script_version_id") == current_id else {}
            visual = visual if visual.get("script_version_id") == current_id else {}
    visual_by_id = {entry["scene_id"]: entry for entry in visual.get("scenes", [])}
    full_audio = audio.get("purpose") == "full_narration_alignment_pending" or audio.get("schema_version") == 3
    audio_by_id = {} if full_audio else {entry["scene_id"]: entry for entry in audio.get("scenes", [])}
    sections, total, voiced = [], 0.0, 0
    full_player = ""
    if full_audio and audio.get("scenes"):
        full_player, total = render_audio(directory, audio["scenes"][0], {"narration": script["narration_text"]})
        full_player = '<section><h2>Полная озвучка</h2>' + full_player + '<p>Точные границы фрагментов пока не определены; сопоставление с аудио не реализовано.</p></section>'
        voiced = int(total > 0)
    elif audio.get("schema_version") == 3 and audio.get("audio_path"):
        full_player, total = render_audio(directory, {**audio, "narration": audio.get("narration_text")},
                                         {"narration": script.get("narration_text")})
        label = "Текст и привязки проверены." if audio.get("status") == "completed" else "Аудио не прошло весь блок проверки; см. причину остановки."
        full_player = '<section><h2>Полная озвучка · голос создан ИИ</h2>' + full_player + f'<p>{label}</p></section>'
        voiced = int(total > 0)
    for scene in plan.get("scenes", []):
        section, duration = render_scene(scene, visual_by_id.get(scene["id"], {}), audio_by_id.get(scene["id"], {}), directory)
        sections.append(section)
        total += duration
        voiced += duration > 0
    sources = "".join(f'<li>{link(item["url"], item.get("title") or item["url"])}</li>' for item in research.get("sources", []))
    facts = "".join(f'<li>{escape(item.get("text", item.get("statement", "")))} '
                    + " · ".join(link(source["url"], source.get("title") or source["url"]) for source in item.get("sources", item.get("evidence", [])))
                    + '</li>' for item in research.get("facts", []))
    warnings = "".join(f'<li>{escape(item)}</li>' for item in research.get("warnings", research.get("notes", [])))
    blockers = "".join(f'<li>{escape(item)}</li>' for item in research.get("blocking_issues", research.get("missing_questions", [])))
    legacy_notes = "".join(f'<li>{escape(item)}</li>' for item in research.get("limitations", []))
    legacy_notice = ("Классификация сохранена в предупреждениях и блокирующих проблемах выше."
                     if research.get("schema_version") == 2 else
                     "Классификация требует явной переоценки; оговорки не считаются автоматически блокирующими.")
    legacy_section = (f'<h3>Исходные оговорки старого исследования</h3><ul>{legacy_notes}</ul>'
                      f'<p>{legacy_notice}</p>') if legacy_notes else ""
    verification = read_optional(directory, "verification.json")
    if not verification:
        versions = sorted(directory.glob("verification_[1-3].json"))
        verification = read_optional(directory, versions[-1].name) if versions else {}
    script_status = (script.get("verification_status") or verification.get("status") or "Проверка не запускалась") if script else "Проверка не запускалась"
    if script_state:
        from script_report import result_label
        script_status = result_label(script_state)
    fragment_table = ""
    if script.get("fragments"):
        from script_report import fragments_table
        fragment_table = fragments_table(script)
    topic = research.get("topic", state.get("topic", "Smartgram"))
    reason = state.get("reason") or script_state.get("stop_reason") or visual.get("error") or ""
    duration_text = (f"Фактическая сумма созданной озвучки: {total:.3f} с. "
                     f"Озвучено сцен: {voiced} из {len(plan.get('scenes', []))}.") if voiced else "Озвучка ещё не создана; фактическая длительность неизвестна."
    if full_audio and voiced:
        timing = "Границы фрагментов привязаны к словам Deepgram." if audio.get("status") == "completed" and audio.get("schema_version") == 3 else "Успешная привязка фрагментов ещё не подтверждена."
        duration_text = f"Фактическая длительность общей озвучки: {total:.3f} с. {timing}"
    page = '''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Smartgram — результат запуска</title><style>
body{max-width:1100px;margin:auto;padding:30px 20px;background:#f4f6f9;color:#1e293b;font:17px/1.6 system-ui,sans-serif}
section,.summary{background:white;border-radius:14px;padding:24px;margin:24px 0}a{color:#215e9f}h1{line-height:1.2}
.chosen{max-height:520px;max-width:100%;display:block;margin:auto}.script{white-space:pre-wrap}.meta,.credit{color:#58667a;font-size:14px}
audio{width:100%}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:16px}.card img{width:100%;height:180px;object-fit:contain}
.card{border:1px solid #ddd;padding:12px;border-radius:10px}.badge{background:#eee;padding:4px 8px}.selected .badge{background:#d8f0df}.rejected .badge{background:#fae4e1}
</style><main>'''
    page += (f'<h1>{escape(topic)}</h1><div class="summary"><p>Режим: {escape(state.get("mode", research.get("mode", "не указан")))} · '
             f'Статус: {escape(state.get("status", "не указан"))} · этап: {escape(state.get("stage", "не указан"))}</p>'
             f'<p><strong>Причина остановки:</strong> {escape(reason or "Не зафиксирована")}</p><p><strong>{duration_text}</strong></p>'
             '<p>Монтаж не выполнялся, MP4 не создан. Оценка ИИ требует просмотра человеком.</p></div>'
             f'<section><h2>Исследование</h2><p>Статус: {escape(research.get("status", "не завершено"))}</p>'
             f'<p>{escape(research.get("explanation", ""))}</p>'
             f'<h3>Предупреждения (не блокируют ролик)</h3><ul>{warnings}</ul>'
             f'<h3>Блокирующие проблемы исследования</h3><ul>{blockers}</ul>{legacy_section}'
             '<h3>Собранные факты</h3><p>Эти материалы исследования ещё не означают прохождение отдельной проверки утверждений.</p>'
             f'<ol>{facts}</ol></section>'
             f'<section><h2>Полный сценарий</h2><p>Проверка: {escape(script_status)}</p>'
             f'<p class="script">{escape(script.get("narration_text", script.get("text", "Сценарий ещё не создан.")))}</p>{fragment_table}</section>'
             f'<section><h2>Источники исследования</h2><ul>{sources}</ul></section>'
             + full_player + ("".join(sections) or ('<section>Материалы ещё не подготовлены.</section>' if script.get("schema_version") == 2
                                                   else '<section>План сцен ещё не создан.</section>')) + '</main></html>')
    destination = output or directory / "result.html"
    atomic_write(destination, page.encode("utf-8"))
    return destination


def main() -> int:
    from html_report_sync import sync_generated_report

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        path = build_result_page(args.run_directory, args.output)
        print(f"Страница результата: {path}")
    except (OSError, ValueError, KeyError, TypeError, VisualError, VoiceoverError) as error:
        message = str(error) if isinstance(error, (VisualError, VoiceoverError)) else "Не удалось прочитать результаты или записать страницу."
        print(message, file=sys.stderr)
        return 1
    return 0 if sync_generated_report(path) else 1


if __name__ == "__main__":
    raise SystemExit(main())
