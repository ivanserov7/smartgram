"""Озвучка трёх сохранённых сценариев. Обычный повтор возобновляет ту же рабочую партию."""

import argparse
import json
import shutil
import sys
from pathlib import Path

from file_io import atomic_write_json
from html_report_sync import sync_generated_report
from research_batch import DEFAULT_TOPICS, load_topics
from research import ResearchError
from script_workflow import approved_script
from voice_errors import VoiceoverError
from voiceover import digest, voiceover_from_file
from voiceover_report import build_voiceover_report
from voice_settings import load_voiceover_settings


def select_sources(script_batch):
    data = json.loads((script_batch / "batch.json").read_text())
    entries = {entry["topic"]: entry for entry in data["entries"]}
    selected = []
    for topic in load_topics(DEFAULT_TOPICS):
        try:
            baseline = Path(entries[topic]["directory"])
            script = approved_script(baseline)
            baseline_state = json.loads((baseline / "script_state.json").read_text())
            date = baseline_state["versions"][-1]["created_at_utc"]
            candidates = [(date, baseline, script)]
            for path in script_batch.parent.rglob("script_state.json"):
                if any(part.startswith("voiceover_batch_") for part in path.parts):
                    continue
                try:
                    state = json.loads(path.read_text())
                    if state.get("topic") != topic or state.get("status") != "approved":
                        continue
                    newer = state["versions"][-1]["created_at_utc"]
                    if newer > date:
                        candidates.append((newer, path.parent, approved_script(path.parent)))
                except (OSError, ValueError, KeyError, TypeError):
                    continue
            chosen_date, directory, current = max(candidates, key=lambda candidate: candidate[0])
            selected.append({"topic": topic, "source_directory": str(directory.resolve()), "source_script_version_id": current["script_version_id"],
                             "source_date_utc": chosen_date, "source_fragments": current["fragments"],
                             "selection": "newer_approved_version" if directory != baseline else "requested_batch"})
        except Exception as error:
            detail = str(error) if isinstance(error, (ResearchError, VoiceoverError)) else type(error).__name__
            selected.append({"topic": topic, "source_directory": None, "error": f"Нельзя прочитать текущий одобренный сценарий: {detail}."})
    return selected


def copy_working_script(entry, destination):
    source = Path(entry["source_directory"])
    if (destination / "voiceover_state.json").exists() or (destination / "script_state.json").exists():
        raise VoiceoverError("Рабочая история существует без voiceover_origin.json. Она не перезаписана; бюджет не сброшен. Восстановите происхождение копии.")
    destination.mkdir(parents=True, exist_ok=True)
    required = {"research.json", "script_state.json", "script.json", "verification.json", "run.json"}
    names = required | {"research_origin.json", "script_usage.json"}
    names |= {path.name for path in source.glob("script_[0-9]*.json")}
    names |= {path.name for path in source.glob("verification_[0-9]*.json")}
    for name in names:
        path = source / name
        if path.exists():
            shutil.copyfile(path, destination / name)
        elif name in required:
            raise VoiceoverError(f"Нет обязательного {name} в источнике сценария.")
    atomic_write_json(destination / "voiceover_origin.json", entry)


def run_batch(script_batch=None, resume=None, output_root=None, run=voiceover_from_file):
    if resume:
        directory = Path(resume)
        batch = json.loads((directory / "batch.json").read_text())
    else:
        sources = select_sources(Path(script_batch))
        identity = [{name: entry.get(name) for name in ("topic", "source_directory", "source_script_version_id")} for entry in sources]
        _, settings = load_voiceover_settings(require_keys=False)
        synthesis_config = {key:value for key,value in settings.snapshot()["settings"].items() if key.startswith("tts_")}
        identifier = digest({"sources":identity,"synthesis":synthesis_config})[:16]
        directory = Path(output_root or Path(__file__).parent / "outputs") / ("voiceover_batch_" + identifier)
        if not directory.exists():
            # Совместимость с уже созданными партиями: изменение формулы имени не оплачивает всё заново.
            compatible = []
            for existing in directory.parent.glob("voiceover_batch_*/batch.json"):
                saved = json.loads(existing.read_text())
                saved_identity = [{name: entry.get(name) for name in ("topic", "source_directory", "source_script_version_id")} for entry in saved.get("entries", [])]
                if saved.get("source_batch") == str(Path(script_batch).resolve()) and saved_identity == identity and saved.get("synthesis_config") == synthesis_config:
                    compatible.append(existing.parent)
            if len(compatible) > 1:
                raise VoiceoverError("Найдено несколько партий тех же сценариев. Укажите конкретную папку через --resume; новые запросы не отправлены.")
            if compatible:
                directory = compatible[0]
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "batch.json"
        batch = json.loads(path.read_text()) if path.exists() else {"schema_version": 1, "source_batch": str(Path(script_batch).resolve()), "synthesis_config":synthesis_config,"entries": sources}
    for number, entry in enumerate(batch["entries"], 1):
        print(f"Озвучка {number}/{len(batch['entries'])}: {entry['topic']}", flush=True)
        working = directory / f"topic_{number}"
        entry["directory"] = str(working.resolve())
        try:
            if not entry.get("source_directory"):
                raise VoiceoverError(entry.get("error") or "Исходный сценарий недоступен.")
            if not (working / "voiceover_origin.json").exists():
                copy_working_script(entry, working)
            run(working / "script.json")
            entry.update(status="completed", error=None)
        except Exception as error:
            reason = str(error) if isinstance(error, VoiceoverError) else f"Технический сбой ({type(error).__name__}); новая генерация не запускалась автоматически."
            entry.update(status="stopped", error=reason)
            print(reason, flush=True)
        atomic_write_json(directory / "batch.json", batch)
        build_voiceover_report(batch, directory / "report.html")
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--script-batch", type=Path)
    source.add_argument("--resume", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        print("Dry-run: один WAV выбранного провайдера → Nova-2 + независимый OpenAI ASR → GPT-проверка и привязка. Файлы и API не используются.")
        return 0
    try:
        directory = run_batch(args.script_batch, args.resume, args.output_root)
        copied = sync_generated_report(directory / "report.html")
        print(f"Отчёт: {directory / 'report.html'}")
        entries = json.loads((directory / "batch.json").read_text())["entries"]
        return 0 if copied and all(entry.get("status") == "completed" for entry in entries) else 1
    except (OSError, ValueError, KeyError, VoiceoverError) as error:
        print(str(error) if isinstance(error, VoiceoverError) else "Не удалось прочитать/сохранить партию озвучки.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
