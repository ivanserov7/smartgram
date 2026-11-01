"""Написание + проверка, сохранение версий и один общий бюджет доработок."""

import hashlib
import json
import math
import time
from pathlib import Path

from fact_checker import check_script, validate_verification
from file_io import atomic_write_json
from research import ResearchError
from research_requests import now_iso
from script_config import ScriptSettings
from scriptwriter import narration_text, require_usable_research, script_identity, validate_script, write_script


def research_identity(research: dict) -> str:
    return hashlib.sha256(json.dumps(research, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def save_script_state(directory, state):
    atomic_write_json(directory / "script_state.json", state)


def approved_script(directory: Path, demo=False) -> dict:
    """Потребители принимают только текущую версию с соответствующим одобрением."""
    try:
        state = json.loads((directory / "script_state.json").read_text())
        current = state["versions"][-1]
        script = validate_script(current["script"])
        review = validate_verification(current["verification"], script)
        identifier = script_identity(script)
        saved = json.loads((directory / "script.json").read_text())
        if (state["status"] != "approved" or not review["approved"] or current["script_version_id"] != identifier
                or saved["script_version_id"] != identifier or saved["fragments"] != script["fragments"]
                or state["mode"] != ("demo" if demo else "real")):
            raise ValueError
        return {**script, "script_version_id": identifier, "narration_text": narration_text(script), "mode": state["mode"]}
    except (OSError, ValueError, KeyError, TypeError, IndexError, ResearchError):
        raise ResearchError("Нет текущего одобренного fragments-сценария. Старый текстовый сценарий нужно заново создать по сохранённому исследованию.") from None


def load_script_state(research, directory, settings, demo):
    """Проверяет сохранённую историю и возвращает её неизменный бюджет."""
    path = directory / "script_state.json"
    identity = research_identity(research)
    if path.exists():
        try:
            state = json.loads(path.read_text())
            if state["schema_version"] != 2 or state["research_sha256"] != identity or state["mode"] != ("demo" if demo else "real"):
                raise ValueError
            settings = ScriptSettings(**state["settings"])
            if type(state["revisions_used"]) is not int or not 0 <= state["revisions_used"] <= settings.max_revisions:
                raise ValueError
            expected_count = state["revisions_used"] + 1 if state["versions"] else 0
            if len(state["versions"]) != expected_count or not state["versions"] and state["revisions_used"]:
                raise ValueError
            if not isinstance(state["versions"], list) or state["status"] not in {"running", "approved", "needs_revision", "technical_error"}:
                raise ValueError
            if type(state["duration_seconds"]) not in (int, float) or not isinstance(state["duration_feedback_history"], list):
                raise ValueError
            for version in state["versions"]:
                if script_identity(version["script"]) != version["script_version_id"]:
                    raise ValueError
            if state["status"] == "technical_error":
                raise ResearchError("Предыдущий запуск остановлен технической ошибкой; автоматический повтор отключён. " + state["stop_reason"])
        except (ValueError, KeyError, TypeError):
            raise ResearchError("Не удалось продолжить script_state.json: структура или исходное исследование изменились.") from None
    else:
        if (directory / "script.json").exists():
            raise ResearchError("Старый текстовый сценарий несовместим: начните новый запуск сценариста по сохранённому исследованию.")
        state = {"schema_version": 2, "mode": "demo" if demo else "real", "topic": research["topic"],
                 "created_at_utc": now_iso(), "research_sha256": identity, "settings": settings.to_dict(),
                 "revisions_used": 0, "versions": [], "status": "running", "stop_reason": None,
                 "duration_feedback_history": [], "duration_seconds": 0}
    return state, settings


def apply_duration_feedback(state, directory, settings, duration_feedback):
    """Измерение аудио отзывает текущее одобрение, но не стирает историю."""
    measured = duration_feedback.get("measured_duration_seconds")
    if type(measured) not in (int, float) or not math.isfinite(measured) or measured <= 0 or settings.target_min_seconds <= measured <= settings.target_max_seconds:
        raise ResearchError("Нужна измеренная длительность аудио вне целевого диапазона.")
    if not state["versions"] or not state["versions"][-1].get("verification", {}).get("approved"):
        raise ResearchError("Замечание об аудио принимается только для проверенной текущей версии.")
    state["duration_feedback_history"].append(duration_feedback)
    state.update(status="needs_revision", stop_reason=None)
    state["versions"][-1]["duration_feedback"] = duration_feedback
    # Старое одобрение сохраняется в истории, но более не разрешает downstream.
    save_script_state(directory, state)


def run_scriptwriter(research: dict, directory: Path, generate, settings=None, demo=False, duration_feedback=None) -> dict:
    """Возобновление сохраняет общий счётчик; сбой не запускает новые запросы автоматически."""
    require_usable_research(research, demo)
    directory.mkdir(parents=True, exist_ok=True)
    state, settings = load_script_state(research, directory, settings or ScriptSettings(), demo)
    if duration_feedback is not None:
        apply_duration_feedback(state, directory, settings, duration_feedback)
    elif state["status"] == "approved":
        return approved_script(directory, demo)
    save_script_state(directory, state)
    started = time.monotonic()
    try:
        while True:
            previous = state["versions"][-1] if state["versions"] else None
            # Незавершённая проверка при обычном продолжении не означает разрешения повторного API.
            if previous and previous.get("verification") is None:
                raise ResearchError("Предыдущая проверка не завершена; автоматический повтор отключён.")
            if previous and state["revisions_used"] >= settings.max_revisions:
                state.update(status="needs_revision", stop_reason=f"Исчерпан общий бюджет доработок ({settings.max_revisions}).")
                save_script_state(directory, state)
                raise ResearchError(state["stop_reason"])
            feedback = None if previous is None else {"issues": previous["verification"]["issues"],
                                                       "duration": previous.get("duration_feedback")}
            print(f"Сценарист: версия {len(state['versions']) + 1}; доработок использовано {state['revisions_used']}/{settings.max_revisions}.", flush=True)
            script = write_script(research, previous=previous["script"] if previous else None, feedback=feedback,
                                  generate=generate, settings=settings, demo=demo)
            if previous:
                state["revisions_used"] += 1
            version = {"number": len(state["versions"]) + 1, "created_at_utc": now_iso(), "script": script,
                       "script_version_id": script_identity(script), "verification": None, "feedback_received": feedback}
            state["versions"].append(version)
            state.update(status="running", stop_reason=None)
            save_script_state(directory, state)
            atomic_write_json(directory / f"script_{version['number']}.json", {
                "schema_version": 2, "mode": state["mode"], **script, "script_version_id": version["script_version_id"],
                "narration_text": narration_text(script)})
            review = check_script(script, research, generate=generate, settings=settings)
            version["verification"] = review
            atomic_write_json(directory / f"verification_{version['number']}.json", {"schema_version": 2, "mode": state["mode"],
                               **review, "script_version_id": version["script_version_id"]})
            atomic_write_json(directory / "verification.json", {**review, "schema_version": 2, "mode": state["mode"],
                               "script_version_id": version["script_version_id"]})
            if review["approved"]:
                state.update(status="approved", stop_reason=None)
                atomic_write_json(directory / "script.json", {"schema_version": 2, "mode": state["mode"], **script,
                                  "narration_text": narration_text(script), "script_version_id": version["script_version_id"]})
                save_script_state(directory, state)
                return approved_script(directory, demo)
            state["status"] = "needs_revision"
            save_script_state(directory, state)
    except ResearchError as error:
        if state.get("stop_reason") is None:
            state.update(status="technical_error", stop_reason=str(error))
        raise
    finally:
        state["duration_seconds"] += round(time.monotonic() - started, 3)
        save_script_state(directory, state)
