"""Технический адаптер fragments для подбора материалов. Модель здесь не вызывается."""

from pathlib import Path

from dotenv import dotenv_values

from research import ResearchError
from scriptwriter import script_identity, validate_script


def available_material_types() -> list[str]:
    values = dotenv_values(Path(__file__).with_name(".env"), interpolate=False)
    value = values.get("AVAILABLE_MATERIAL_TYPES", "image")
    types = [item.strip() for item in value.split(",")] if isinstance(value, str) else []
    if not types or any(item not in {"image", "video"} for item in types):
        raise ResearchError("AVAILABLE_MATERIAL_TYPES должен содержать image или image,video.")
    return list(dict.fromkeys(types))


def scenes_from_script(script: dict, demo=False) -> dict:
    """Одна запись на смысловой фрагмент; search_query начинается с проверенного visual."""
    draft = validate_script({"fragments": script.get("fragments")})
    identifier = script_identity(draft)
    if script.get("script_version_id") != identifier or script.get("mode") != ("demo" if demo else "real"):
        raise ResearchError("Адаптер материалов требует текущий одобренный сценарий нужного режима.")
    if "image" not in available_material_types():
        raise ResearchError("Для текущего подбора необходимо включить image в AVAILABLE_MATERIAL_TYPES.")
    return {"schema_version": 2, "script_version_id": identifier, "mode": script["mode"],
            "verification_status": "demo_passed" if demo else "passed", "available_material_types": available_material_types(),
            "timing_status": "alignment_not_implemented", "scenes": [
                {"id": number, "fragment_index": number - 1, "script_version_id": identifier,
                 "narration": fragment["text"], "visual_description": fragment["visual"],
                 "search_query": fragment["visual"], "material_type": "image",
                 "estimated_duration_seconds": None}
                for number, fragment in enumerate(draft["fragments"], 1)]}
