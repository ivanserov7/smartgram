"""Одна сравнительная запись трёх прежних сценариев; без новых исследований/доработок."""

import argparse
import hashlib
import json
from pathlib import Path

from audio_files import inspect_wav
from file_io import atomic_write_json
from script_workflow import approved_script
from voice_errors import VoiceoverError
from voice_settings import load_voiceover_settings
from voiceover import VoiceoverRun
from voiceover_batch import copy_working_script
from voiceover_report import build_voiceover_report, optional
from voice_costs import summarize_voice_costs


def run_comparison(directory, source_batch):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    keys, settings = load_voiceover_settings(require_keys=False)
    if (settings.tts_provider, settings.tts_model, settings.tts_voice) != (
        "gemini",
        "gemini-3.8-flash-tts",
        "Kore",
    ):
        raise VoiceoverError(
            "Для этой сравнительной партии нужен Gemini gemini-3.8-flash-tts / Kore. Проверьте реальные переменные окружения и .env; fallback запрещён."
        )
    source = json.loads((Path(source_batch) / "batch.json").read_text())
    synthesis = {
        name: value
        for name, value in settings.snapshot()["settings"].items()
        if name.startswith("tts_")
    }
    path = directory / "batch.json"
    if path.exists():
        batch = json.loads(path.read_text())
        if batch["synthesis_settings"] != synthesis:
            raise VoiceoverError(
                "Настройки сравнительного синтеза изменились. Старые записи не перезаписаны; для нового опыта выберите новую папку."
            )
    else:
        batch = {
            "schema_version": 1,
            "mode": "real",
            "comparison_mode": True,
            "source_batch": str(Path(source_batch).resolve()),
            "synthesis_settings": synthesis,
            "entries": source["entries"],
        }
        atomic_write_json(path, batch)
    for index, entry in enumerate(batch["entries"], 1):
        print(f'Gemini {index}/{len(batch["entries"])}: {entry["topic"]}', flush=True)
        working = directory / f"topic_{index}"
        entry["directory"] = str(working.resolve())
        script = None
        try:
            script = approved_script(Path(entry["source_directory"]))
            if script["script_version_id"] != entry["source_script_version_id"]:
                raise VoiceoverError(
                    "Сценарий источника изменился. Новый текст автоматически не выбран."
                )
            if not (working / "voiceover_origin.json").exists():
                copy_working_script(entry, working)
            entry["narration_sha256"] = hashlib.sha256(
                script["narration_text"].encode()
            ).hexdigest()
            missing = [
                name
                for name in ("GEMINI_API_KEY", "DEEPGRAM_API_KEY", "OPENAI_API_KEY")
                if not keys.get(name)
            ]
            if missing:
                raise VoiceoverError(
                    "Отсутствуют "
                    + ", ".join(missing)
                    + ". Google: https://aistudio.google.com/api-keys; ключи не присылайте в чат."
                )
            VoiceoverRun(working, settings, keys).run(comparison_mode=True)
            entry.update(status="completed", error=None)
        except VoiceoverError as error:
            entry.update(status="stopped", error=str(error))
            print(str(error), flush=True)
            manifest = optional(working / "audio_manifest.json")
            if script is not None:
                manifest.setdefault("narration_text", script["narration_text"])
                manifest.setdefault("provider", "gemini")
                manifest.setdefault("model", settings.tts_model)
                manifest.setdefault("voice", settings.tts_voice)
                manifest.setdefault("style", settings.tts_style)
                manifest.setdefault("status", "not_started")
                manifest.setdefault("reason", str(error))
                atomic_write_json(working / "audio_manifest.json", manifest)
            # Даже технически отклонённый настоящий WAV можно дать послушать.
            if not manifest.get("audio_path"):
                candidates = sorted(working.glob("voiceover/*/provider_audio.wav"))
                if candidates and script is not None:
                    try:
                        measured = inspect_wav(candidates[-1].read_bytes())
                        manifest.update(
                            **measured,
                            audio_path=str(candidates[-1].relative_to(working)),
                            provider="gemini",
                            model=settings.tts_model,
                            voice=settings.tts_voice,
                            style=settings.tts_style,
                            status="technical_check_failed",
                            reason=str(error),
                            text_verified=False,
                            narration_text=script["narration_text"],
                        )
                        atomic_write_json(working / "audio_manifest.json", manifest)
                    except VoiceoverError:
                        pass
        batch["costs"] = summarize_voice_costs(directory)
        atomic_write_json(path, batch)
        build_voiceover_report(batch, directory / "report.html")
    return directory / "report.html"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--directory", type=Path, default=Path("outputs/gemini_migration_20261101")
    )
    parser.add_argument(
        "--source-batch",
        type=Path,
        default=Path("outputs/voiceover_batch_c04a8905ad9d821e"),
    )
    args = parser.parse_args()
    try:
        print("Отчёт:", run_comparison(args.directory, args.source_batch))
    except (VoiceoverError, OSError, ValueError, KeyError) as error:
        parser.exit(
            1,
            (
                str(error)
                if isinstance(error, VoiceoverError)
                else "Не удалось прочитать/сохранить сравнительную партию.\n"
            ),
        )
