"""Озвучка проверенных сцен с сохранением прогресса и повторным использованием WAV."""

import hashlib
import json
from pathlib import Path

from audio_files import atomic_write, atomic_write_json, demo_wav, inspect_wav
from gemini_tts import VoiceoverError, VoiceSettings, load_voice_settings, synthesize_wav


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


def audio_fingerprint(scene: dict, settings: VoiceSettings, mode: str) -> str:
    """Точный текст, голос, стиль, формат и версия запроса входят в ключ кеша."""
    data = {"scene_id": scene["id"], "narration": scene["narration"],
            "settings": settings.synthesis_options(), "mode": mode}
    if scene.get("script_version_id"):
        data["script_version_id"] = scene["script_version_id"]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def load_cached_audio(audio_path: Path, metadata_path: Path, expected: dict) -> dict | None:
    """Проверяет метаданные, контрольную сумму и WAV; длительность измеряет заново."""
    try:
        metadata = read_json(metadata_path)
        if any(metadata.get(key) != value for key, value in expected.items()):
            return None
        measured = inspect_wav(audio_path.read_bytes())
        if measured["audio_sha256"] != metadata.get("audio_sha256"):
            return None
        return {**metadata, **measured, "reused": True}
    except (OSError, VoiceoverError):
        return None


def prepare_scene_audio(scene: dict, directory: Path, settings: VoiceSettings, api_key: str,
                        demo=False, synthesize=synthesize_wav) -> dict:
    """Записывает WAV и квитанцию сцены до обновления общего манифеста."""
    mode = "demo" if demo else "real"
    fingerprint = audio_fingerprint(scene, settings, mode)
    audio_directory = directory / ("audio_demo" if demo else "audio")
    audio_directory.mkdir(parents=True, exist_ok=True)
    audio_path = audio_directory / f"scene_{scene['id']:03d}_{fingerprint}.wav"
    metadata_path = audio_path.with_suffix(".json")
    expected = {"scene_id": scene["id"], "narration": scene["narration"], "fingerprint": fingerprint,
                "model": settings.model, "voice": settings.voice, "settings": settings.synthesis_options(),
                "mode": mode, "audio_kind": "test_silence" if demo else "generated_speech",
                "audio_path": audio_path.relative_to(directory).as_posix()}
    if scene.get("script_version_id"):
        expected["script_version_id"] = scene["script_version_id"]
    cached = load_cached_audio(audio_path, metadata_path, expected)
    if cached is not None:
        return cached
    content = synthesize(scene["narration"], settings, api_key)
    measured = inspect_wav(content)
    atomic_write(audio_path, content)
    entry = {**expected, **measured, "reused": False}
    # Отдельная квитанция позволяет восстановиться даже после потери общего манифеста.
    atomic_write_json(metadata_path, entry)
    return entry


def generate_scene_audio(scenes: list[dict], directory: Path, demo=False, purpose="scenes") -> Path:
    """Сохраняет прогресс после каждой сцены; при сбое останавливается без повторов."""
    if demo:
        print("ТЕСТОВАЯ ОЗВУЧКА: создаётся только тишина, не настоящая речь. Сети и API нет.")
        api_key, settings, synthesize = "", VoiceSettings(model="demo-no-api"), demo_wav
    else:
        api_key, settings = load_voice_settings()
        synthesize = synthesize_wav
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "audio_manifest.json"
    manifest = {"mode": "demo" if demo else "real", "purpose": purpose, "status": "running",
                "audio_kind": "test_silence" if demo else "generated_speech",
                "duration_source": "measured_wav_frames", "scenes": [], "total_duration_seconds": 0.0}
    if scenes and scenes[0].get("script_version_id"):
        manifest.update(script_version_id=scenes[0]["script_version_id"], timing_status="alignment_not_implemented")
    atomic_write_json(path, manifest)
    for scene in scenes:
        try:
            entry = prepare_scene_audio(scene, directory, settings, api_key, demo=demo, synthesize=synthesize)
        except (VoiceoverError, OSError) as error:
            manifest.update(status="error", failed_scene_id=scene["id"],
                            reason=str(error) if isinstance(error, VoiceoverError) else "Ошибка сохранения аудио на диск.")
            atomic_write_json(path, manifest)
            raise VoiceoverError(f"Озвучка сцены {scene['id']} остановлена. {manifest['reason']} Прогресс: {path}") from None
        manifest["scenes"].append(entry)
        manifest["total_duration_seconds"] = sum(item["duration_seconds"] for item in manifest["scenes"])
        atomic_write_json(path, manifest)
        action = "переиспользовано" if entry["reused"] else "сохранено"
        print(f"Аудио сцены {scene['id']}: {action}, {entry['duration_seconds']:.3f} с.")
    manifest["status"] = "demo_completed" if demo else "completed"
    atomic_write_json(path, manifest)
    print(f"Манифест с измеренными длительностями: {path}")
    return path


def voiceover_from_file(path: Path, demo=False) -> Path:
    """Повторный запуск только озвучки: сохраняет аудио рядом с исходным планом."""
    path = Path(path)
    if path.name == "script.json":
        return voiceover_from_script(path, demo)
    plan = load_verified_scenes(path, demo)
    if plan.get("schema_version") == 2:
        return voiceover_from_script(path.with_name("script.json"), demo)
    return generate_scene_audio(plan["scenes"], path.parent, demo=demo)


def voiceover_from_script(path: Path, demo=False) -> Path:
    """Новый контракт: одна дорожка из всех text, без visual и придуманных таймкодов."""
    from script_workflow import approved_script
    from research import ResearchError
    try:
        script = approved_script(path.parent, demo)
    except ResearchError as error:
        raise VoiceoverError(str(error)) from None
    return generate_scene_audio([{"id": 1, "narration": script["narration_text"],
                                  "script_version_id": script["script_version_id"]}],
                                path.parent, demo=demo, purpose="full_narration_alignment_pending")
