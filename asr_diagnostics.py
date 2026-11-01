"""Явная диагностика ASR на сохранённом WAV. Никогда не генерирует голос/сценарий."""

import argparse
import json
from dataclasses import replace
from pathlib import Path

from audio_files import inspect_wav
from file_io import atomic_write, atomic_write_json
from script_workflow import approved_script
from voice_alignment import validate_check, validate_words
from voice_api import VoiceAPI
from voice_errors import VoiceoverError
from voice_settings import load_voiceover_settings
from voiceover import VoiceoverRun


def audit_audio(source):
    """Сверяет файл, сохранённую квитанцию и исходный ответ самого Deepgram."""
    source = Path(source).resolve()
    manifest = json.loads((source / "audio_manifest.json").read_text())
    path = (source / manifest["audio_path"]).resolve()
    if not path.is_relative_to(source):
        raise VoiceoverError("WAV выходит за пределы исходного запуска.")
    measured = inspect_wav(path.read_bytes())
    receipt = json.loads(path.with_name("audio.json").read_text())
    raw = json.loads(path.with_name("deepgram.json").read_text())
    ledger = json.loads((source / "deepgram_usage.json").read_text())
    requests = [item for item in ledger["requests"] if item.get("response") == raw]
    stored_words = json.loads(path.with_name("words.json").read_text())["words"]
    original_words = raw["results"]["channels"][0]["alternatives"][0]["words"]
    converted_words = [{key: value for key, value in word.items() if key != "word_id"} for word in stored_words]
    expected_hash = measured["audio_sha256"]
    matches = {"receipt": receipt["audio_sha256"] == expected_hash,
               "manifest": manifest["audio_sha256"] == expected_hash,
               "provider": raw["metadata"].get("sha256") == expected_hash,
               "upload": any(item["request"]["audio_sha256"] == expected_hash for item in requests)}
    if not all(matches.values()):
        raise VoiceoverError("Не совпали хеши исходного WAV/квитанции/ответа Deepgram. Новых запросов нет.")
    server_duration = raw["metadata"]["duration"]
    if abs(server_duration - measured["duration_seconds"]) > .02:
        raise VoiceoverError("Deepgram и WAV сообщают разную длительность. Новых запросов нет.")
    return path, {"source_directory": str(source), "audio_path": str(path), **measured,
                  "hash_matches": matches, "provider_duration_seconds": server_duration,
                  "original_words_unchanged_by_conversion": converted_words == original_words,
                  "receipt_duration_matches": receipt["duration_seconds"] == measured["duration_seconds"],
                  "original_response_path": str(path.with_name("deepgram.json")),
                  "original_response_matches_ledger": bool(requests), "original_metadata": raw["metadata"]}


def timing_result(raw, duration, tolerance):
    """Возвращает причину отказа без исправления секунд в исходном ответе."""
    try:
        words = validate_words(raw, duration, tolerance)
        return {"valid": True, "reason": None, "words": words}
    except VoiceoverError as error:
        return {"valid": False, "reason": str(error), "words": None}


def diagnose(source, destination, model, keys, settings, api=None):
    """Новая диагностическая квитанция отдельно от старого кеша. Сбой не повторяется."""
    source, destination = Path(source), Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    path, audit = audit_audio(source)
    script = approved_script(source)
    settings = replace(settings, asr_model=model, asr_language="ru")
    api = api or VoiceAPI(destination, settings, keys)
    run = VoiceoverRun(destination, settings, keys, api=api)
    atomic_write_json(destination / "audit.json", audit)
    # Нельзя случайно повторить оплаченный POST из-за смены модели проверки текста.
    existing = [item for item in run.state["operations"].values() if item["stage"] == "diagnostic_transcribe"]
    if existing:
        entry = existing[0]
        if entry["request"]["model"] != model or len(existing) != 1:
            raise VoiceoverError("Эта папка уже относится к другой диагностике. Старый результат не заменён.")
        if entry["status"] != "completed":
            raise VoiceoverError("Диагностический POST ранее не завершился успешно; автоматический повтор отключён.")
        raw = entry["result"]
        if raw.get("metadata", {}).get("sha256") != audit["audio_sha256"]:
            raise VoiceoverError("Сохранённая диагностика относится к другому WAV.")
    else:
        raw = run.operation("diagnostic_transcribe", {"audio_sha256": audit["audio_sha256"], "model": model},
            {"model": model, "language": "ru", "fresh_from_original_cache": True},
            lambda: api.transcribe(path, script["script_version_id"], audit["audio_sha256"]))
    atomic_write_json(destination / "deepgram.json", raw)
    timing = timing_result(raw, audit["duration_seconds"], settings.timing_tolerance_seconds)
    # Сервер подтвердил получение тех же байтов; одного имени файла недостаточно.
    if raw.get("metadata", {}).get("sha256") != audit["audio_sha256"]:
        timing = {"valid": False, "reason": "SHA-256 в новом ответе Deepgram не совпал с отправленным WAV.", "words": None}
    result = {"model": model, "language": "ru", "source_directory": str(source.resolve()),
              "script": script, "audit": audit, "metadata": raw.get("metadata"), "timing": timing,
              "transcript": raw["results"]["channels"][0]["alternatives"][0]["transcript"],
              "raw_words": raw["results"]["channels"][0]["alternatives"][0]["words"]}
    atomic_write_json(destination / "diagnostic.json", result)
    return result


def review_saved(destination, keys, settings, api=None):
    """Только текст/привязка: даже при ошибке чтения нет вызовов TTS или сценариста."""
    destination = Path(destination)
    result = json.loads((destination / "diagnostic.json").read_text())
    settings = replace(settings, asr_model=result["model"], asr_language="ru")
    path, audit = audit_audio(result["source_directory"])
    if audit["audio_sha256"] != result["audit"]["audio_sha256"]:
        raise VoiceoverError("Исходный WAV изменился после диагностики.")
    raw = json.loads((destination / "deepgram.json").read_text())
    state = json.loads((destination / "voiceover_state.json").read_text())
    operations = [item for item in state["operations"].values() if item["stage"] == "diagnostic_transcribe"]
    if (len(operations) != 1 or operations[0]["status"] != "completed"
            or operations[0]["request"]["model"] != result["model"] or operations[0]["result"] != raw):
        raise VoiceoverError("Ответ ASR не совпал с сохранённой операцией/моделью. Смешивание расшифровок запрещено.")
    timing = timing_result(raw, audit["duration_seconds"], settings.timing_tolerance_seconds)
    if not timing["valid"]:
        raise VoiceoverError(timing["reason"])
    atomic_write(destination / "audio.wav", path.read_bytes())
    run = VoiceoverRun(destination, settings, keys, api=api)
    audio = {**audit, "audio_path": "audio.wav", "script_version_id": result["script"]["script_version_id"],
             "narration_text": result["script"]["narration_text"]}
    run.manifest.update(**audio, asr_model=result["model"], generation_permitted=False, rewrite_permitted=False,
                        voice_quality="Не проверена; остаётся отдельной нерешённой задачей.")
    try:
        review, mapped, words = run.check_audio(result["script"], audio, destination, raw_response=raw)
        # При расхождении можно сохранить предложенную привязку отдельно, не одобряя текст.
        if not mapped:
            _, mapped = validate_check(review, result["script"], words["words"], audit["duration_seconds"],
                                       settings.timing_tolerance_seconds, confirmed=True)
        approved = review["decision"] == "Можно использовать"
        run.state.update(status="completed" if approved else "needs_review", reason=None if approved else review["decision"])
        run.manifest.update(status=run.state["status"], reason=run.state["reason"], review=review,
                            timing_validated=True, text_verified=approved, transcript=words["transcript"],
                            words=words["words"], fragments=mapped if approved else [],
                            provisional_fragments=[] if approved else mapped,
                            total_duration_seconds=audit["duration_seconds"])
    except VoiceoverError as error:
        run.state.update(status="review_error", reason=str(error))
        run.manifest.update(status="review_error", reason=str(error), text_verified=False, fragments=[])
    run.save()
    return run.manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--model", choices=("nova-3", "nova-2"))
    parser.add_argument("--review-only", action="store_true", help="Проверить сохранённую диагностику без ASR/TTS и переписывания.")
    args = parser.parse_args()
    try:
        keys, settings = load_voiceover_settings()
        if args.review_only:
            manifest = review_saved(args.destination, keys, settings)
            print(json.dumps({"status": manifest["status"], "reason": manifest["reason"], "review": manifest.get("review"),
                              "fragments": manifest["fragments"]}, ensure_ascii=False))
            return
        if args.source is None or args.model is None:
            parser.error("Для нового ASR нужны --source и --model.")
        result = diagnose(args.source, args.destination, args.model, keys, settings)
        print(json.dumps({"model": result["model"], "timing_valid": result["timing"]["valid"],
                          "reason": result["timing"]["reason"], "transcript": result["transcript"],
                          "last_word": result["raw_words"][-1], "metadata": result["metadata"]}, ensure_ascii=False))
    except (VoiceoverError, OSError, ValueError, KeyError) as error:
        parser.exit(1, f"Диагностика остановлена: {error}\n")


if __name__ == "__main__":
    main()
