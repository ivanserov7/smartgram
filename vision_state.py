"""Кеш ответов Vision и отдельная история реальных обращений к OpenAI."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import image_files
from commons_api import VisualError
from file_io import atomic_write_json


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def cache_key(scene, candidates, model, parameters, prompt_version, instructions, schema) -> str:
    """Учитывает текст, сами файлы, метаданные, настройки API и действующие ограничения."""
    return digest({
        "scene": {name: scene.get(name) for name in ("id", "narration", "visual_description", "material_type", "script_version_id")},
        "images": [{name: candidate.get(name) for name in (
            "candidate_id", "file_title", "file_sha256", "analysis_image_sha256", "eligible_for_video",
            "eligibility_reason", "license", "license_url", "license_id", "author", "source_page",
        )} for candidate in candidates],
        "model": model, "parameters": parameters, "prompt_version": prompt_version,
        "instructions": instructions, "schema": schema,
        "limits": {"width": image_files.MIN_WIDTH, "height": image_files.MIN_HEIGHT,
                   "bytes": image_files.MAX_IMAGE_BYTES, "pixels": image_files.MAX_PIXELS,
                   "preview_size": image_files.PREVIEW_SIZE, "preview_version": image_files.PREVIEW_VERSION},
    })


def cached_response(directory: Path, key: str, images_sent: list) -> dict | None:
    # Файлы уже заново открыты и проверены вызывающей функцией, до обращения к кешу.
    try:
        saved = json.loads((directory / "vision_cache" / f"{key}.json").read_text())
        response = saved["response"]
        if saved["key"] != key or response["status"] != "model_reviewed" or response["images_sent"] != images_sent:
            return None
        checks = response["checks"]
        ids = [item["candidate_id"] for item in checks]
        if len(ids) != len(images_sent) or set(ids) != {item["candidate_id"] for item in images_sent}:
            return None
        return {**response, "cache_hit": True, "request_count": 0,
                "original_usage": response.get("usage"), "usage": None}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_response(directory: Path, key: str, response: dict) -> None:
    folder = directory / "vision_cache"
    folder.mkdir(exist_ok=True)
    atomic_write_json(folder / f"{key}.json", {"key": key, "response": response})


def usage_history(directory: Path) -> list[dict]:
    path = directory / "vision_usage.json"
    if not path.exists():
        return []
    # Повреждённую историю нельзя молча заменить пустой и потерять сведения о расходе.
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise VisualError("Не удалось прочитать vision_usage.json; история расхода не перезаписана.") from None
    if not isinstance(saved, dict) or not isinstance(saved.get("requests"), list):
        raise VisualError("Повреждён vision_usage.json.")
    return saved["requests"]


def record_usage(directory: Path, operation: str, result: dict) -> None:
    history = usage_history(directory)
    telemetry = result.get("telemetry") or {}
    if telemetry.get("call_id"):
        history = [entry for entry in history if (entry.get("telemetry") or {}).get("call_id") != telemetry["call_id"]]
    history.append({"operation": operation, "created_at": datetime.now(timezone.utc).isoformat(),
                    **{name: result.get(name) for name in (
                        "response_id", "model", "configured_model", "usage", "status", "error", "request_count", "telemetry",
                    )}})
    atomic_write_json(directory / "vision_usage.json", {"requests": history})
