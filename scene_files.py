"""Чтение старых и новых планов материалов без импорта генераторов речи."""

import json
from pathlib import Path
from voice_errors import VoiceoverError

def read_json(path: Path) -> dict:
    """Не пропускает повреждённые файлы в дальнейшую обработку."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise VoiceoverError(f"Не удалось прочитать {path.name}. Проверьте файл JSON.") from None
    if not isinstance(data, dict):
        raise VoiceoverError(f"Некорректный {path.name}: ожидается объект.")
    return data


def validate_scenes(plan: dict, demo=False) -> None:
    """Озвучивает только проверенный план и не смешивает реальный и тестовый режимы."""
    if plan.get("mode") != ("demo" if demo else "real"):
        raise VoiceoverError("Режим scenes.json не совпадает с запуском. Для тестового плана нужен --demo.")
    if plan.get("verification_status") != ("demo_passed" if demo else "passed"):
        raise VoiceoverError("Сценарий плана не прошёл проверку. Озвучка запрещена.")
    scenes = plan.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise VoiceoverError("Некорректный scenes.json: нужен непустой список scenes.")
    scene_ids = set()
    for scene in scenes:
        if not isinstance(scene, dict) or type(scene.get("id")) is not int or scene["id"] <= 0:
            raise VoiceoverError("Некорректный scenes.json: номер каждой сцены должен быть положительным целым.")
        if scene["id"] in scene_ids:
            raise VoiceoverError("Некорректный scenes.json: номера сцен повторяются.")
        scene_ids.add(scene["id"])
        if not isinstance(scene.get("narration"), str) or not scene["narration"].strip():
            raise VoiceoverError(f"Сцена {scene['id']}: narration должен быть непустой строкой.")


def load_verified_scenes(path: Path, demo=False) -> dict:
    """Поддерживает старые планы, если рядом есть соответствующий принятый сценарий."""
    plan = read_json(path)
    if plan.get("schema_version") == 2:
        from script_workflow import approved_script
        from scene_planner import scenes_from_script
        from research import ResearchError
        try:
            current = scenes_from_script(approved_script(path.parent, demo), demo)
            if plan != current:
                raise ResearchError("Материалы относятся к другой версии сценария.")
        except ResearchError as error:
            raise VoiceoverError(str(error)) from None
    if "verification_status" not in plan:
        script = read_json(path.with_name("script.json"))
        report = read_json(path.with_name("verification.json"))
        expected = "demo_passed" if demo else "passed"
        try:
            lines = script["lines"]
            line_ids = [line_id for scene in plan["scenes"] for line_id in scene["line_ids"]]
            text_by_id = {line["id"]: line["text"] for line in lines}
            same_narration = all(scene["narration"] == " ".join(text_by_id[line_id] for line_id in scene["line_ids"])
                                 for scene in plan["scenes"])
            valid = (line_ids == [line["id"] for line in lines] and same_narration
                     and script["verification_status"] == report["status"] == expected
                     and script["mode"] == report["mode"] == plan["mode"])
        except (KeyError, TypeError):
            valid = False
        if not valid:
            raise VoiceoverError("Старый план сцен не соответствует проверенному сценарию. Пересоздайте план.")
        plan["verification_status"] = expected
    validate_scenes(plan, demo)
    return plan

