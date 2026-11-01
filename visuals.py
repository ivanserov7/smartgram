"""Подбор изображений Commons по scenes.json без OpenAI и без озвучки."""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from commons_api import VisualError, VisualServiceError, accepted_license, create_commons_client, download_image, search_images
from file_io import atomic_write, atomic_write_json
from visual_sequence import legacy_source_in_use
from image_files import MIN_HEIGHT, MIN_WIDTH, candidate_problem, crop_quality, inspect_image, relevance_score


def load_scenes(path: Path, demo=False) -> dict:
    """Проверяет поля, нужные только подбору; предыдущие этапы не запускает."""
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise VisualError("Не удалось прочитать scenes.json. Проверьте путь и формат.") from None
    if not isinstance(plan, dict) or not isinstance(plan.get("scenes"), list) or not plan["scenes"]:
        raise VisualError("В scenes.json нужен непустой список scenes.")
    if plan.get("schema_version") == 2:
        from voiceover import load_verified_scenes
        from voice_errors import VoiceoverError
        try:
            load_verified_scenes(path, demo)
        except VoiceoverError as error:
            raise VisualError(str(error)) from None
    mode = plan.get("mode", "real")
    if not isinstance(mode, str) or mode not in {"real", "demo"}:
        raise VisualError("В scenes.json указан неизвестный mode.")
    if plan.get("mode") == "demo" and not demo:
        raise VisualError("Для тестовых сцен нужен --demo; настоящего поиска для них не будет.")
    ids = set()
    for scene in plan["scenes"]:
        if not isinstance(scene, dict) or type(scene.get("id")) is not int or scene["id"] <= 0 or scene["id"] in ids:
            raise VisualError("Номера сцен должны быть положительными целыми и не повторяться.")
        ids.add(scene["id"])
        if not isinstance(scene.get("material_type"), str) or scene["material_type"] not in {"image", "video"}:
            raise VisualError(f"Сцена {scene['id']}: material_type должен быть image или video.")
        for name in ("search_query", "visual_description"):
            if not isinstance(scene.get(name), str) or not scene[name].strip():
                raise VisualError(f"Сцена {scene['id']}: нужен непустой {name}.")
        if "title" in scene and not isinstance(scene["title"], str):
            raise VisualError(f"Сцена {scene['id']}: title должен быть строкой.")
    return plan


def scene_fingerprint(scene: dict, mode: str) -> str:
    # При изменении правил ранжирования увеличивайте selection_version.
    data = {key: scene.get(key, "") for key in ("id", "search_query", "title", "visual_description", "material_type", "narration", "script_version_id")}
    data.update(mode=mode, selection_version=2, min_width=MIN_WIDTH, min_height=MIN_HEIGHT)
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def cached_selection(directory: Path, receipt: Path, fingerprint: str) -> dict | None:
    """Повторно открывает файл, проверяет хеш и атрибуцию. Не доверяет одному пути."""
    try:
        entry = json.loads(receipt.read_text(encoding="utf-8"))
        if not isinstance(entry, dict) or entry.get("fingerprint") != fingerprint or entry.get("status") != "selected":
            return None
        source = entry["selected"]
        if accepted_license(source["license"], source["license_url"]) != source["license_id"] or candidate_problem(source):
            return None
        path = (directory / source["local_path"]).resolve()
        if not path.is_relative_to((directory / "visuals").resolve()):
            return None
        content = path.read_bytes()
        measured = inspect_image(content)
        if measured["file_sha256"] != source["file_sha256"]:
            return None
        entry["selected"].update(measured)
        entry["reused"] = True
        return entry
    except (OSError, ValueError, KeyError, TypeError, VisualError):
        return None


def rank_candidates(scene: dict, candidates: list[dict]) -> tuple[list[dict], list[dict]]:
    """Отдельно сохраняет все причины отсева и порядок допустимых кандидатов."""
    audits = []
    ranked = []
    for candidate in candidates:
        problem = candidate_problem(candidate)
        score, matched = relevance_score(scene, candidate)
        audit = {**candidate, "relevance_score": score, "matched_terms": matched,
                 "status": "rejected" if problem or not score else "candidate",
                 "reason": problem or ("Недостаточное совпадение с запросом и описанием сцены." if not score else "")}
        audits.append(audit)
        if not problem and score:
            ranked.append(audit)
    ranked.sort(key=lambda item: (item["relevance_score"], crop_quality(item["width"], item["height"])), reverse=True)
    return audits, ranked


def select_image(scene: dict, directory: Path, client, mode="real") -> dict:
    """Сначала соответствие метаданных, затем качество; повреждённый кандидат пропускается."""
    fingerprint = scene_fingerprint(scene, mode)
    receipt = directory / "visuals" / f"scene_{scene['id']:03d}_{fingerprint}.json"
    cached = cached_selection(directory, receipt, fingerprint)
    if cached is not None and not legacy_source_in_use(directory,cached["selected"],scene["id"]):
        return cached
    entry = {"scene_id": scene["id"], "search_query": scene["search_query"], "material_type": "image",
             "mode": mode, "fingerprint": fingerprint, "status": "needs_visual", "reused": False,
             "selection_method": "metadata_overlap_then_resolution", "visual_check_status": "not_performed",
             "candidates": [], "selected": None}
    try:
        candidates = search_images(scene["search_query"], client)
    except VisualError as error:
        entry["reason"] = str(error)
        entry["status"] = "error"
        return entry
    entry["candidates"], ranked = rank_candidates(scene, candidates)
    for candidate in ranked:
        if legacy_source_in_use(directory,candidate,scene["id"]):
            candidate.update(status="rejected",reason="Исходник уже назначен другой сцене этого ролика.")
            continue
        try:
            content = download_image(candidate["download_url"], client)
            measured = inspect_image(content)
            if {"JPEG": "image/jpeg", "PNG": "image/png"}[measured["format"]] != candidate["mime"]:
                raise VisualError("Формат скачанного файла не совпадает с метаданными кандидата.")
        except VisualServiceError as error:
            entry.update(status="error", reason=str(error))
            return entry
        except VisualError as error:
            candidate.update(status="rejected", reason=str(error))
            continue
        if legacy_source_in_use(directory,{**candidate,**measured},scene["id"]):
            candidate.update(status="rejected",reason="Хеш совпал с исходником другой сцены.")
            continue
        suffix = ".jpg" if measured["format"] == "JPEG" else ".png"
        path = directory / "visuals" / f"scene_{scene['id']:03d}_{fingerprint}{suffix}"
        atomic_write(path, content)
        selected = {**candidate, **measured, "local_path": path.relative_to(directory).as_posix()}
        selected["attribution"] = (f"{candidate['title']} — {candidate['author']}. "
                                   f"{candidate['license']}: {candidate['license_url']}. Источник: {candidate['source_page']}")
        candidate["status"] = "selected"
        selected["status"] = "selected"
        candidate.update(measured, local_path=selected["local_path"], attribution=selected["attribution"])
        entry.update(status="selected", selected=selected,
                     reason=(f"Максимальное допустимое совпадение метаданных ({candidate['relevance_score']:.3f}); "
                             f"совпали слова: {', '.join(candidate['matched_terms'])}. При равенстве оценок "
                             f"учтено разрешение для кадра 9:16. Файл открыт: {measured['width']}×{measured['height']}. "
                             "Это предварительный подбор, визуальная проверка не выполнялась."))
        atomic_write_json(receipt, entry)
        return entry
    entry["reason"] = ("Поиск не вернул кандидатов." if not candidates else
                       "Нет подходящего изображения после проверки лицензии, соответствия и файла; нужна ручная подготовка визуала.")
    return entry


def search_round(entry: dict, number: int) -> dict:
    """Снимок раунда без вложенной истории: сохраняет также отклонённые варианты."""
    return {"round": number, **{name: entry.get(name) for name in (
        "search_query", "status", "reason", "candidates", "selected",
    )}}


def collect_visuals(path: Path, demo=False) -> Path:
    """Сохраняет результат каждой сцены, включая пустой поиск и неподдержанное видео."""
    path = Path(path)
    plan = load_scenes(path, demo)
    directory = path.parent
    (directory / "visuals").mkdir(exist_ok=True)
    mode = "demo" if demo else "real"
    manifest = {"mode": mode, "status": "running", "selection_method": "metadata_only",
                "visual_check_status": "not_performed", "created_at": datetime.now(timezone.utc).isoformat(), "scenes": []}
    if plan.get("script_version_id"):
        manifest["script_version_id"] = plan["script_version_id"]
    manifest_path = directory / "visual_manifest.json"
    try:
        previous = json.loads(manifest_path.read_text())
    except (OSError, ValueError):
        previous = {}
    atomic_write_json(manifest_path, manifest)
    # В демонстрации клиент вообще не создаётся: никаких фоновых сетевых запросов.
    if demo:
        print("ТЕСТ: поиск Commons не выполняется; изображения не подставляются.")
    client = create_commons_client() if not demo else None
    try:
        for scene in plan["scenes"]:
            if scene["material_type"] == "video":
                entry = {"scene_id": scene["id"], "search_query": scene["search_query"], "material_type": "video",
                         "status": "video_not_implemented", "reason": "Подбор видео пока не реализован. Изображение вместо видео не подставлялось."}
            elif demo:
                entry = {"scene_id": scene["id"], "search_query": scene["search_query"], "material_type": "image",
                         "status": "needs_visual", "reason": "Демонстрация: настоящий поиск не выполнялся."}
            else:
                fingerprint = scene_fingerprint(scene, mode)
                saved = next((item for item in previous.get("scenes", [])
                              if item.get("scene_id") == scene["id"] and item.get("plan_fingerprint") == fingerprint), None)
                # Уже выполненный пустой/второй поиск не начинается заново при продолжении.
                if saved and (saved.get("search_rounds", 0) >= 2 or saved.get("status") != "selected"):
                    entry = saved
                    if entry.get("status") == "searching":
                        entry.update(status="error", reason="Предыдущий поиск прерван. Автоматический повтор запрещён.")
                else:
                    # Резервируем попытку до HTTP: прерывание процесса не обнулит лимит раундов.
                    pending = {"scene_id": scene["id"], "plan_fingerprint": fingerprint,
                               "search_query": scene["search_query"], "status": "searching",
                               "search_rounds": 2 if saved else 1}
                    manifest["scenes"].append(pending)
                    atomic_write_json(manifest_path, manifest)
                    entry = select_image(scene, directory, client, mode)
                    manifest["scenes"].pop()
                    number = saved.get("search_rounds", 1) if saved and entry.get("reused") else (2 if saved else 1)
                    rounds = saved.get("rounds", []) if saved else []
                    if not entry.get("reused"):
                        rounds = rounds + [search_round(entry, number)]
                    entry.update(plan_fingerprint=fingerprint, search_rounds=number, rounds=rounds)
            entry.update(mode=mode, visual_check_status="not_performed")
            manifest["scenes"].append(entry)
            atomic_write_json(manifest_path, manifest)
            if entry["status"] == "error":
                raise VisualError(entry["reason"])
            print(f"Визуал сцены {scene['id']}: {entry['status']}.")
            if entry["status"] != "selected":
                print(f"Причина: {entry['reason']}")
    except (OSError, VisualError) as error:
        manifest.update(status="error", reason=str(error) if isinstance(error, VisualError) else "Не удалось сохранить визуальные материалы на диск.")
        atomic_write_json(manifest_path, manifest)
        raise
    finally:
        if client is not None:
            client.close()
    manifest["status"] = "ready_for_visual_review" if all(entry["status"] == "selected" for entry in manifest["scenes"]) else "needs_visual"
    atomic_write_json(manifest_path, manifest)
    print(f"Манифест визуальных материалов: {manifest_path}")
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenes-file", type=Path, required=True)
    parser.add_argument("--demo", action="store_true", help="Без сети; сохранить потребность в визуальных материалах")
    arguments = parser.parse_args()
    try:
        collect_visuals(arguments.scenes_file, demo=arguments.demo)
    except VisualError as error:
        print(f"Подбор изображений остановлен: {error}", file=sys.stderr)
        return 1
    except OSError:
        print("Не удалось сохранить визуальные материалы. Проверьте папку и место на диске.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
