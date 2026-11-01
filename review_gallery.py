"""HTML-галерея настоящих результатов visual_manifest.json для ручного просмотра."""

import argparse
import base64
import hashlib
import html
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from commons_api import API_URL, MAX_API_BYTES, VisualError, bounded_get, create_commons_client, download_image
from file_io import atomic_write, atomic_write_json
from image_files import inspect_image


def escape(value) -> str:
    return html.escape(str(value), quote=True)


def link(url: str, label: str) -> str:
    """Не позволяет метаданным превратить ссылку в исполняемый код HTML."""
    try:
        parsed = urlsplit(url)
        safe = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
    except (ValueError, TypeError):
        safe = False
    if not safe:
        return escape(label)
    return f'<a href="{escape(url)}" target="_blank" rel="noopener noreferrer">{escape(label)}</a>'


def image_data(content: bytes) -> str:
    # Для карточек разрешены маленькие превью. Условия выбора изображения не меняются.
    measured = inspect_image(content, minimum_width=1, minimum_height=1)
    mime = "image/jpeg" if measured["format"] == "JPEG" else "image/png"
    return f"data:{mime};base64," + base64.b64encode(content).decode("ascii")


def local_image_data(directory: Path, relative_path: str) -> str:
    """Манифест не должен позволять чтение файлов за пределами папки запуска."""
    path = (directory / relative_path).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise VisualError("Путь изображения выходит за пределы папки запуска.")
    return image_data(path.read_bytes())


def fetch_previews(manifest: dict, directory: Path) -> dict:
    """Получает адреса небольших превью из API, без конструирования URL вручную."""
    titles = list(dict.fromkeys(candidate["file_title"] for scene in manifest["scenes"] for candidate in scene["candidates"]))
    if not titles:
        return {}
    previews = {}
    preview_directory = directory / "gallery_previews"
    preview_directory.mkdir(exist_ok=True)
    with create_commons_client() as client:
        content, _ = bounded_get(client, API_URL, MAX_API_BYTES, {
            "action": "query", "format": "json", "formatversion": 2, "prop": "imageinfo",
            "titles": "|".join(titles), "iiprop": "url|mime|thumbmime", "iiurlwidth": 500,
        })
        result = json.loads(content)
        if not isinstance(result, dict) or "error" in result:
            raise VisualError("API не предоставил превью галереи.")
        for page in result.get("query", {}).get("pages", []):
            info = (page.get("imageinfo") or [{}])[0]
            if info.get("mime") not in {"image/jpeg", "image/png"}:
                continue
            url = info.get("thumburl") or info.get("url")
            if not url:
                continue
            try:
                image = download_image(url, client)
                measured = inspect_image(image, minimum_width=1, minimum_height=1)
                suffix = ".jpg" if measured["format"] == "JPEG" else ".png"
                name = hashlib.sha256(page["title"].encode()).hexdigest()[:20] + suffix
                path = preview_directory / name
                atomic_write(path, image)
                previews[page["title"]] = {"local_path": path.relative_to(directory).as_posix(), "url": url, "status": "downloaded"}
            except VisualError as error:
                # При новом 403 прекращаем обращения, как попросил пользователь.
                if "403" in str(error):
                    raise
                previews[page["title"]] = {"status": "unavailable", "reason": str(error)}
    atomic_write_json(directory / "gallery_previews.json", {"purpose": "review_only", "previews": previews})
    return previews


def credits(candidate: dict) -> str:
    return (f'<p class="credit">{escape(candidate["author"])} · '
            f'{link(candidate["license_url"], candidate["license"] or "Лицензия не указана")} · '
            f'{link(candidate["source_page"], "Страница Commons")}</p>')


def render_candidate(candidate: dict, directory: Path, previews: dict) -> str:
    status = candidate["status"]
    labels = {"selected": "Выбран", "rejected": "Отклонён", "candidate": "Не выбран · резерв"}
    reason = candidate["reason"] or ("Изображение выбрано для сцены." if status == "selected" else
             "Остался в резерве: выбран кандидат выше в рейтинге; скачивание этого варианта для монтажа не проверялось.")
    preview = previews.get(candidate["file_title"], {})
    if preview.get("status") == "downloaded":
        image = local_image_data(directory, preview["local_path"])
        picture = f'<img loading="lazy" src="{image}" alt="{escape(candidate["title"])}">'
    else:
        picture = '<p class="no-preview">Превью недоступно. Откройте страницу Commons.</p>'
    ai = candidate.get("ai_review")
    ai_details = (f'<p><strong>Оценка ИИ: {escape(ai["score"])}/100 · {escape(ai["verdict"])}</strong></p>'
                  f'<p>ИИ видит: {escape(ai["observed_content"])}</p>') if ai else ""
    return (f'<article class="card {escape(status)}">{picture}<div class="card-body">'
            f'<span class="badge">{labels.get(status, escape(status))}</span><h3>{escape(candidate["title"])}</h3>'
            f'{ai_details}<p>{escape(reason)}</p><p class="meta">Совпадение метаданных: {candidate["relevance_score"]:.3f} · '
            f'Размер по API: {escape(candidate["width"])}×{escape(candidate["height"])}</p>{credits(candidate)}'
            f'<details><summary>Описание из Commons</summary><p>{escape(candidate["description"])}</p></details></div></article>')


def render_gallery(manifest: dict, directory: Path, previews: dict) -> str:
    sections = []
    selected_count = 0
    for scene in manifest["scenes"]:
        selected = scene.get("selected")
        chosen = '<p>Выбранного изображения нет.</p>'
        if selected:
            selected_count += 1
            image = local_image_data(directory, selected["local_path"])
            chosen = f'<img class="chosen-image" src="{image}" alt="{escape(selected["title"])}">{credits(selected)}'
        cards = "".join(render_candidate(candidate, directory, previews) for candidate in scene["candidates"])
        api = scene.get("api", {})
        usage = api.get("usage") or {}
        api_details = (f'<p class="meta">Модель: {escape(api.get("model", "не вызывалась"))} · '
                       f'входных токенов: {escape(usage.get("input_tokens", "не сообщены"))} · '
                       f'выходных: {escape(usage.get("output_tokens", "не сообщены"))}</p>') if api else ""
        scene_text = f'<p><strong>Текст сцены:</strong> {escape(scene["scene_text"])}</p>' if scene.get("scene_text") else ""
        sections.append(f'<section><h2>Сцена {escape(scene["scene_id"])}: <code>{escape(scene["search_query"])}</code></h2>'
                        f'{scene_text}{api_details}'
                        f'<div class="chosen"><h3>Выбранное изображение</h3>{chosen}<p>{escape(scene["reason"])}</p></div>'
                        f'<h3>Все кандидаты ({len(scene["candidates"])})</h3><div class="grid">{cards}</div></section>')
    notice = ("OpenAI получил сами изображения и текст сцены. Здесь показаны его оценки и объяснения. "
              "Распознавание вида животного может ошибаться; проверьте результат перед использованием.") if manifest.get("selection_method") == "openai_vision" else (
              "Предварительный подбор по метаданным. Посмотрите изображения и причины выбора.")
    return '''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Smartgram — Wikimedia Commons</title><style>
*{box-sizing:border-box}body{margin:0;background:#f3f5f8;color:#192535;font:16px/1.55 system-ui,sans-serif}main{max-width:1440px;margin:auto;padding:40px 24px}
h1{font-size:36px;margin:0}h2{margin:48px 0 20px}h3{margin:12px 0;font-size:18px}a{color:#235fa4}code{font-size:.85em}
.notice,.chosen{background:white;border:1px solid #dce3ed;border-radius:16px;padding:24px}.notice{margin-top:20px}.chosen{border-left:5px solid #297a59}
.chosen-image{display:block;max-width:100%;max-height:560px;margin:20px auto;object-fit:contain}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:20px}
.card{background:white;border:1px solid #dce3ed;border-radius:14px;overflow:hidden}.card img{width:100%;height:220px;object-fit:contain;background:#e9eef4;display:block}
.card-body{padding:18px}.badge{display:inline-block;border-radius:30px;padding:4px 10px;font-size:13px;background:#edf0f5}.selected .badge{background:#d8f0df;color:#175735}
.rejected .badge{background:#fae4e1;color:#82352b}.credit,.meta{color:#59687a;font-size:13px;overflow-wrap:anywhere}.no-preview{padding:20px;min-height:180px}details{margin-top:14px;font-size:14px}
</style><main><h1>Smartgram · Wikimedia Commons</h1>''' + (
        f'<p>Настоящий поиск · сцен: {len(manifest["scenes"])} · изображений выбрано: {selected_count}</p>'
        f'<div class="notice">{notice} '
        'Превью отклонённых вариантов показаны только для сравнения и не добавлены в материалы ролика. '
        'Лицензии и атрибуцию необходимо проверить перед публикацией. Все изображения встроены в этот HTML.</div>'
        + "".join(sections) + '</main></html>')


def main() -> int:
    from html_report_sync import sync_generated_report

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("mode") != "real":
            raise VisualError("Галерея настоящих результатов требует mode: real.")
        if manifest.get("selection_method") == "openai_vision":
            # Для AI-отчёта показываем ровно те файлы, которые были переданы модели, без сети.
            previews = {candidate["file_title"]: {"status": "downloaded", "local_path": candidate["analysis_image_path"]}
                        for scene in manifest["scenes"] for candidate in scene["candidates"] if candidate.get("analysis_image_path")}
        else:
            previews = fetch_previews(manifest, args.manifest.parent)
        atomic_write(args.output, render_gallery(manifest, args.manifest.parent, previews).encode("utf-8"))
    except httpx.HTTPStatusError as error:
        print(f"Галерея не создана: Wikimedia вернул HTTP {error.response.status_code}. Повторов нет.", file=sys.stderr)
        return 1
    except (VisualError, httpx.HTTPError, ValueError, KeyError, TypeError, OSError) as error:
        message = str(error) if isinstance(error, VisualError) else "Не удалось получить превью или сохранить галерею."
        print(f"Галерея не создана: {message}", file=sys.stderr)
        return 1
    print(f"Галерея создана: {args.output}")
    return 0 if sync_generated_report(args.output) else 1


if __name__ == "__main__":
    raise SystemExit(main())
