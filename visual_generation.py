"""Недостающие объяснения: точные элементы рисует код, иллюстрацию — Image API."""

import base64
import os
import time
import httpx
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape
import textwrap
import math

from dotenv import dotenv_values
from PIL import Image, ImageDraw, ImageFont
from openai import OpenAI, APIError

from file_io import atomic_write, atomic_write_json
from production_state import (
    ROOT,
    ProductionError,
    ProductionServiceError,
    digest,
    sha,
    inside,
    read,
)
from image_files import inspect_image
from openai_meter import OpenAIMeter
from billing_api import redact

PROMPT_VERSION = "created-review-v2-associative"


def image_estimate(usage, model="gpt-image-2.5-flare"):
    """Standard Flare; это оценка по usage, а не подтверждённое списание."""
    if model != "gpt-image-2.5-flare" or not usage:
        return None
    details = usage.get("input_tokens_details") or {}
    cached = details.get("cached_tokens_details") or {}
    text, image = details.get("text_tokens", 0), details.get("image_tokens", 0)
    ct, ci = cached.get("text_tokens", 0), cached.get("image_tokens", 0)
    if details.get("cached_tokens", 0) and not cached:
        return None
    if not details or not usage.get("output_tokens"):
        return None
    return (
        (text - ct) * 5
        + ct * 1.25
        + (image - ci) * 8
        + ci * 2
        + usage["output_tokens"] * 30
    ) / 1_000_000


def generate_image(directory, settings, prompt):
    """Ровно один HTTP-вызов. SDK retries=0; недоступная модель не заменяется."""
    key = os.environ.get(
        "OPENAI_API_KEY",
        dotenv_values(ROOT / ".env", interpolate=False).get("OPENAI_API_KEY"),
    )
    if not key or not key.strip():
        raise ProductionServiceError("OPENAI_API_KEY отсутствует; генерация не отправлена.")
    parameters = {
        k: settings[k] for k in ("model", "quality", "n", "size", "output_format")
    }
    if parameters["quality"] not in {"medium", "low"} or parameters["n"] != 1:
        raise ProductionError("Для создания разрешены medium/явный low и n=1.")
    if (
        parameters["size"] not in {"1024x1024", "1024x1536"}
        or parameters["output_format"] != "jpeg"
    ):
        raise ProductionError("Неподдерживаемые параметры непрозрачной иллюстрации.")
    record = {
        "step": "visual_generate",
        "parameters": parameters,
        "prompt_sha256": digest(prompt),
    }
    meter = OpenAIMeter(directory, "visuals", key, record=record)
    meter.telemetry["step"] = "visual_generate"
    meter.telemetry["parameters"] = parameters
    meter.save()
    started = time.monotonic()
    try:
        transport = httpx.Client(
            event_hooks={"request": [meter.on_request], "response": [meter.on_response]}
        )
        with OpenAI(
            api_key=key,
            max_retries=0,
            timeout=settings["timeout_seconds"],
            http_client=transport,
        ) as client:
            response = client.images.generate(prompt=prompt, **parameters)
        usage = response.usage.model_dump(mode="json") if response.usage else None
        meter.telemetry.update(
            status="completed",
            model=parameters["model"],
            usage=usage,
            duration_seconds=time.monotonic() - started,
            ended_at_utc=datetime.now(timezone.utc).isoformat(),
            provider_request_id=getattr(response, "_request_id", None),
        )
        estimate = image_estimate(usage, parameters["model"])
        record.update(
            usage=usage,
            estimate_usd=estimate,
            estimate_status=(
                "estimated_not_billed" if estimate is not None else "unknown"
            ),
        )
        meter.save()  # Usage сохраняется до декодирования/проверки картинки.
        data = base64.b64decode(response.data[0].b64_json, validate=True)
        return data, dict(record, telemetry=None)
    except Exception as error:
        details = redact(getattr(error, "body", None) or str(error), (key,))
        meter.processing_failed(error)
        meter.telemetry.update(
            error={
                "type": type(error).__name__,
                "http_status": getattr(error, "status_code", None),
                "details": details,
            },
            duration_seconds=time.monotonic() - started,
            ended_at_utc=datetime.now(timezone.utc).isoformat(),
            provider_request_id=(
                getattr(error, "request_id", None)
                or meter.telemetry.get("provider_request_id")
            ),
        )
        for attempt in meter.telemetry["attempts"]:
            if attempt["ended_at_utc"] is None:
                attempt["ended_at_utc"] = meter.telemetry["ended_at_utc"]
        meter.save()
        cls = ProductionServiceError if isinstance(error, APIError) else ProductionError
        raise cls(
            f"Image API: {type(error).__name__}: {details}; автоматический повтор отключён."
        ) from None


def diagram_image(spec, folder):
    """Одинаковая схема в SVG и PNG: надписи, число узлов и стрелки задаёт код."""
    labels = spec["labels"]
    if not 2 <= len(labels) <= 6 or any(not x.strip() or len(x) > 120 for x in labels):
        raise ProductionError(
            "Схема требует 2–6 коротких подписей из подтверждённого содержания."
        )
    links = spec.get("links", [])
    if any(
        type(e["source"]) is not int
        or type(e["target"]) is not int
        or not 0 <= e["source"] < len(labels)
        or not 0 <= e["target"] < len(labels)
        or e["source"] == e["target"]
        for e in links
    ):
        raise ProductionError("Неверные связи точной схемы.")
    # Уровни DAG позволяют показать четыре независимых спутника и один приёмник.
    levels = [0] * len(labels)
    for _ in range(len(labels)):
        changed = False
        for edge in links:
            a, b = edge["source"], edge["target"]
            if levels[b] <= levels[a]:
                levels[b] = levels[a] + 1
                changed = True
        if not changed:
            break
    if max(levels) >= len(labels):
        raise ProductionError("Циклическая схема требует другого явного макета.")
    width, height = 1600, 2844
    font_path = Path(
        os.environ.get("SUBTITLE_FONT_PATH")
        or dotenv_values(ROOT / ".env", interpolate=False).get("SUBTITLE_FONT_PATH")
        or "/System/Library/Fonts/Supplemental/Arial.ttf"
    )
    if not font_path.is_file():
        raise ProductionError("Не найден SUBTITLE_FONT_PATH для локальной схемы.")
    font = ImageFont.truetype(str(font_path), 38)
    image = Image.new("RGB", (width, height), "#102035")
    draw = ImageDraw.Draw(image)
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"><rect width="100%" height="100%" fill="#102035"/>'
    ]
    boxes = {}
    for level in sorted(set(levels)):
        members = [i for i, value in enumerate(levels) if value == level]
        for rank, index in enumerate(members):
            # До четырёх независимых объектов размещаем сеткой 2×2.
            columns = min(2, len(members))
            rows = (len(members) + columns - 1) // columns
            box_width = 1360 if columns == 1 else 640
            x = 120 + (rank % columns) * 720
            y = 200 + level * (2100 / max(1, max(levels))) + (rank // columns) * 320
            if max(levels) == 0:
                y = 200 + (rank // columns) * 600
            boxes[index] = (int(x), int(y), int(x + box_width), int(y + 250))
    for edge in links:
        a, b = boxes[edge["source"]], boxes[edge["target"]]
        start = ((a[0] + a[2]) / 2, a[3] + 8)
        end = ((b[0] + b[2]) / 2, b[1] - 16)
        angle = math.atan2(end[1] - start[1], end[0] - start[0])
        length = 30
        arrow = [
            end,
            (
                end[0] - length * math.cos(angle - 0.5),
                end[1] - length * math.sin(angle - 0.5),
            ),
            (
                end[0] - length * math.cos(angle + 0.5),
                end[1] - length * math.sin(angle + 0.5),
            ),
        ]
        draw.line((start, end), fill="#a8d8ff", width=7)
        draw.polygon(arrow, fill="#a8d8ff")
        svg.append(
            f'<path d="M{start[0]} {start[1]}L{end[0]} {end[1]}" stroke="#a8d8ff" stroke-width="7"/>'
        )
        svg.append(
            '<polygon points="'
            + " ".join(f"{x},{y}" for x, y in arrow)
            + '" fill="#a8d8ff"/>'
        )
    for index, label in enumerate(labels):
        x, y, right, bottom = boxes[index]
        draw.rounded_rectangle(
            (x, y, right, bottom), radius=30, fill="#245079", outline="#a8d8ff", width=3
        )
        svg.append(
            f'<rect x="{x}" y="{y}" width="{right-x}" height="250" rx="30" fill="#245079" stroke="#a8d8ff"/>'
        )
        lines = textwrap.wrap(label, width=26 if right - x < 1000 else 55)
        if len(lines) > 4:
            raise ProductionError("Подпись не помещается без уменьшения шрифта.")
        for line_index, line in enumerate(lines):
            line_y = y + 25 + line_index * 48
            draw.text((x + 30, line_y), line, font=font, fill="white")
            svg.append(
                f'<text x="{x+30}" y="{line_y+38}" fill="white" font-family="Arial" font-size="38">{escape(line)}</text>'
            )
    svg.append("</svg>")
    image.save(folder / "diagram.png")
    atomic_write(folder / "diagram.svg", "".join(svg).encode("utf-8"))
    return folder / "diagram.png", folder / "diagram.svg"


def creation_identity(shot, settings, feedback=None):
    return {
        "spec": shot["creation"],
        "essential": shot["must_show"],
        "text": shot["text"],
        "settings": settings,
        "review_version": PROMPT_VERSION,
        "feedback": feedback,
        **({"repair_id": shot["repair_id"], "task_version": shot.get("task_version")} if shot.get("repair_id") else {}),
    }


def create_material(directory, shot, settings, journal, feedback=None):
    """Ключ включает смысл, параметры и версию проверки; сбой не повторяется при resume."""
    spec = shot["creation"]
    identity = creation_identity(shot, settings, feedback)
    folder = Path(directory) / "visual_materials" / "created" / digest(identity)[:24]
    folder.mkdir(parents=True, exist_ok=True)

    def perform():
        source_svg = None
        preparation = {"scale": 1.0, "scope": "original"}
        if spec["kind"] == "diagram":
            raw, source_svg = diagram_image(spec, folder)
            record = {
                "usage": None,
                "estimate_usd": 0,
                "parameters": {"executor": "python_svg"},
                "prompt_sha256": digest(spec),
            }
            origin = "deterministic"
            final = raw
        else:
            prompt = spec["prompt"] + ("\nИсправить: " + feedback if feedback else "")
            data, record = generate_image(directory, settings, prompt)
            inspect_image(data, minimum_width=1, minimum_height=1)
            raw = folder / "generated.jpg"
            atomic_write(raw, data)
            # Оригинал остаётся отдельно; разрешённая обработка записывается в происхождение.
            with Image.open(raw) as original:
                original.load()
                image = original.convert("RGB")
                if settings.get("generated_upscale_limit") == 1.25:
                    if image.size != (1024, 1536):
                        raise ProductionError(
                            "Генератор вернул размер, отличный от разрешённых 1024×1536."
                        )
                    # Без полей и растяжения пропорций: 1280×1920, затем штатный crop 1080×1920.
                    image = image.resize((1280, 1920), Image.Resampling.LANCZOS)
                    preparation = {
                        "scale": 1.25,
                        "scope": "generated_only_user_authorized",
                        "native_size": [1024, 1536],
                        "working_size": [1280, 1920],
                        "padding": False,
                    }
                    final = folder / "portrait.png"
                    image.save(final)
                else:
                    image.thumbnail((1080, 1920))
                    canvas = Image.new("RGB", (1080, 1920), "#102035")
                    canvas.paste(
                        image, ((1080 - image.width) // 2, (1920 - image.height) // 2)
                    )
                    final = folder / "canvas.png"
                    canvas.save(final)
            origin = "generated"
        proof = {
            "origin": origin,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "model": settings["model"] if origin == "generated" else None,
            "parameters": record["parameters"],
            "prompt_sha256": record["prompt_sha256"],
            "usage": record.get("usage"),
            "estimate_usd": record.get("estimate_usd"),
            "estimate_status": (
                "estimated_not_billed" if record.get("estimate_usd") is not None else "unknown"
            ),
            "raw_path": str(raw.relative_to(directory)),
            "raw_sha256": sha(raw),
            "preparation": preparation,
            "spec": spec,
            "review_version": PROMPT_VERSION,
        }
        if source_svg:
            proof.update(
                svg_path=str(source_svg.relative_to(directory)),
                svg_sha256=sha(source_svg),
            )
        proof_path = folder / "provenance.json"
        atomic_write_json(proof_path, proof)
        with Image.open(final) as measured:
            width, height = measured.size
        return {
            "candidate_id": digest(identity)[:24],
            "title": (
                "Созданная тематическая иллюстрация"
                if shot.get("role") == "illustrative"
                else "Созданное визуальное объяснение"
            ),
            "origin": origin,
            "provenance_path": str(proof_path.relative_to(directory)),
            "provenance_sha256": sha(proof_path),
            "original_path": str(final.relative_to(directory)),
            "file_sha256": sha(final),
            "original_width": width,
            "original_height": height,
            "license": None,
            "source_page": None,
            "author": (
                "Smartgram (код)" if origin == "deterministic" else "OpenAI Image API"
            ),
            **proof,
        }

    return journal.operation("visual_create", identity, perform)


def verify_created(source, root):
    proof = inside(root, source["provenance_path"])
    if sha(proof) != source["provenance_sha256"] or source.get("license"):
        raise ProductionError("Повреждено происхождение созданной иллюстрации.")
    saved = read(proof)
    if not isinstance(saved, dict) or any(source.get(key) != value for key, value in saved.items()):
        raise ProductionError("Метаданные иллюстрации не совпадают с сохранённым происхождением.")
    if sha(inside(root, source["raw_path"])) != source["raw_sha256"]:
        raise ProductionError("Изменён исходник генерации.")
    if (
        source.get("svg_path")
        and sha(inside(root, source["svg_path"])) != source["svg_sha256"]
    ):
        raise ProductionError("Изменена точная SVG-схема.")
