"""Один полный WAV → Deepgram → смысловая проверка и привязка фрагментов OpenAI."""

import hashlib
import io
import json
import wave
import time
from pathlib import Path

from audio_files import inspect_wav
from file_io import atomic_write, atomic_write_json
from scene_files import load_verified_scenes, read_json, validate_scenes
from script_workflow import approved_script
from voice_alignment import validate_check, validate_words, text_authority
from voice_factory import create_voice_api
from voice_errors import AudioContractError, AudioAlignmentError, AudioTechnicalError, VoiceoverError, VoiceServiceError
from voice_audio_checks import current_audio_checks
from voice_settings import load_voiceover_settings


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def audio_result(directory, result):
    path = (directory / result["audio_path"]).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise AudioContractError("Аудио выходит за пределы папки запуска.")
    try:
        measured = inspect_wav(path.read_bytes())
    except OSError:
        raise AudioContractError("Ранее оплаченный WAV отсутствует; автоматический платный повтор отключён.") from None
    if measured["audio_sha256"] != result["audio_sha256"]:
        raise AudioContractError("SHA-256 ранее сохранённого WAV изменился; кеш не принят.")
    return path, measured


class VoiceoverRun:
    def __init__(self, directory, settings, keys, api=None):
        self.directory, self.settings, self.keys = Path(directory), settings, keys
        self.snapshot = settings.snapshot()
        self.path = self.directory / "voiceover_state.json"
        self.state = read_json(self.path) if self.path.exists() else {
            "schema_version": 3, "status": "running", "reason": None, "operations": {}, "jobs": [],
            "regenerations_by_version": {}, "rejected_audio_hashes": []}
        if self.state.get("schema_version") != 3 or not isinstance(self.state.get("operations"), dict):
            raise VoiceoverError("Старое состояние озвучки несовместимо. Исходные файлы не изменены.")
        self.api = api or create_voice_api(self.directory, settings, keys)
        self.manifest = {"schema_version": 3, "mode": "real", "status": "running", "reason": None,
                         "audio_kind": "ai_generated_speech", "duration_source": "measured_wav_frames", "fragments": []}

    def save(self):
        atomic_write_json(self.path, self.state)
        atomic_write_json(self.directory / "audio_manifest.json", self.manifest)

    def operation(self, stage, identity, request, function):
        key = digest({"stage": stage, "identity": identity})
        existing = self.state["operations"].get(key)
        if existing:
            if existing["status"] == "completed":
                return existing["result"]
            raise VoiceoverError(f"Операция {stage} ранее не завершилась успешно; списание может быть неизвестно. Автоматический повтор отключён. {existing.get('reason', '')}")
        required = ("GEMINI_API_KEY" if self.settings.tts_provider == "gemini" else "OPENAI_API_KEY") if stage == "synthesize" else "DEEPGRAM_API_KEY" if "transcribe" in stage else "OPENAI_API_KEY"
        if not self.keys.get(required):
            raise VoiceoverError(f"Заполните {required} в локальном .env; запрос не отправлен. Ключи не присылайте в чат.")
        entry = {"stage": stage, "request": request, "status": "started", "result": None}
        self.state["operations"][key] = entry
        self.save()
        retry_count = (self.settings.asr_max_retries if "transcribe" in stage else
                       self.settings.confirm_max_retries if stage == "audio_confirm" else
                       self.settings.check_max_retries if stage == "audio_check" else 0)
        entry["attempts"] = []
        for number in range(retry_count+1):
            attempt = {"number": number+1, "status": "started"}
            entry["attempts"].append(attempt)
            self.save()
            try:
                result = function()
                attempt["status"] = "completed"
                entry.update(status="completed", result=result)
                self.save()
                return result
            except Exception as error:
                reason = str(error) if isinstance(error, VoiceoverError) else "Техническая ошибка операции; результат сохранён."
                attempt.update(status="failed", reason=reason)
                entry.update(status="failed", reason=reason)
                self.save()
                if isinstance(error, VoiceServiceError) and error.retryable and number < retry_count:
                    time.sleep(min(2**number, 4))
                    continue
                raise

    def synthesize(self, script, generation, attempt_directory):
        options = {name: value for name, value in self.snapshot["settings"].items() if name.startswith("tts_")}
        identity = {"script_version_id": script["script_version_id"], "text": script["narration_text"], "settings": options,
                    "prompt": self.snapshot["prompts"]["narrator"], "format": self.snapshot["format"], "generation": generation}
        def generate():
            content, metadata = self.api.synthesize(script, attempt_directory)
            measured = inspect_wav(content)
            path = attempt_directory / "audio.wav"
            atomic_write(path, content)
            with wave.open(io.BytesIO(content), "rb") as wav:
                atomic_write(attempt_directory / "audio.pcm", wav.readframes(wav.getnframes()))
            result = {**metadata, **measured, "audio_path": str(path.relative_to(self.directory)),
                      "script_version_id": script["script_version_id"], "narration_text": script["narration_text"],
                      "settings_snapshot": self.snapshot}
            atomic_write_json(attempt_directory / "audio.json", result)
            return result
        return self.operation("synthesize", identity, identity, generate)

    def independent_transcript(self, path, script, audio_hash, attempt_directory):
        identity = {"audio_sha256": audio_hash, "model": self.settings.confirm_model, "languages": [self.settings.asr_language],
                    "analysis_round": self.state.get("analysis_rounds", {}).get(audio_hash, 0)}
        confirmation = self.operation("audio_confirm", identity, identity,
            lambda: self.api.confirm(path, script["script_version_id"], audio_hash))
        if not isinstance(confirmation, dict) or not isinstance(confirmation.get("text"), str) or not confirmation["text"].strip():
            raise AudioContractError("Независимый OpenAI ASR не вернул непустой текст. Это не ошибка диктора.")
        atomic_write_json(attempt_directory / "openai_transcript.json", confirmation)
        self.manifest["openai_transcript"] = confirmation
        self.save()
        return confirmation

    def check_audio(self, script, audio, attempt_directory, raw_response=None):
        path, measured = audio_result(self.directory, audio)
        audio_hash = measured["audio_sha256"]
        asr_options = {name: value for name, value in self.snapshot["settings"].items() if name.startswith("asr_")}
        # Обе расшифровки обязательны. Ошибка одного сервиса не выдаётся за ошибку чтения.
        raw, asr_error = raw_response, None
        if raw_response is None:
            try:
                raw = self.operation("transcribe", {"audio_sha256": audio_hash, "settings": asr_options,
                                     "analysis_round": self.state.get("analysis_rounds", {}).get(audio_hash, 0)}, asr_options,
                                     lambda: self.api.transcribe(path, script["script_version_id"], audio_hash))
            except VoiceoverError as error:
                asr_error = error
        elif raw.get("metadata", {}).get("sha256") != audio_hash:
            raise AudioContractError("Ответ диагностики относится к другому WAV.")
        confirmation = self.independent_transcript(path, script, audio_hash, attempt_directory)
        if asr_error:
            raise asr_error
        atomic_write_json(attempt_directory / "deepgram.json", raw)
        if raw.get("metadata", {}).get("sha256") not in (None, audio_hash):
            raise AudioContractError("Deepgram вернул SHA-256 другого WAV; проверка не запускается.")
        try:
            alternative = raw["results"]["channels"][0]["alternatives"][0]
            self.manifest["transcript"] = alternative["transcript"]
            atomic_write_json(attempt_directory / "words.json", {"timing_validated": False, **alternative})
            self.save()
        except (KeyError, IndexError, TypeError):
            pass
        early_authority=text_authority(script,(self.manifest.get('transcript',''),confirmation['text']))
        if early_authority['approved']:
            self.manifest.update(text_verified=True,text_confirmation=early_authority)
            self.save()
        try:
            words = validate_words(raw, measured["duration_seconds"], self.settings.timing_tolerance_seconds)
        except AudioContractError as error:
            if early_authority['approved']:
                raise AudioAlignmentError('OpenAI подтвердил текст, но таймкоды Deepgram непригодны. '+str(error),text_verified=True) from None
            raise
        words["timing_validated"] = True
        atomic_write_json(attempt_directory / "words.json", words)
        check_identity = {"verification_version": 2, "audio_sha256": audio_hash,
            "analysis_round": self.state.get("analysis_rounds", {}).get(audio_hash, 0),
            "confirmation_model": self.settings.confirm_model, "asr_model": self.settings.asr_model,
            "script_version_id": script["script_version_id"], "deepgram": words,
            "confirmation": confirmation, "prompt": self.snapshot["prompts"]["check"],
            "settings": {name: value for name, value in self.snapshot["settings"].items() if name.startswith("check_")}}
        feedback = None
        for check_round in (1, 2):
            identity = check_identity if feedback is None else {**check_identity, "validation_feedback": feedback}
            def request_check():
                if feedback is None:
                    return self.api.check(script, words, audio_hash, confirmation)
                return self.api.check(script, words, audio_hash, confirmation, validation_feedback=feedback)
            check = self.operation("audio_check", identity, identity, request_check)
            atomic_write_json(attempt_directory / f"check_{check_round}_raw.json", check)
            try:
                if check.get("verification_version") != 2:
                    raise AudioContractError("Нужен контракт версии 2 с основаниями двух ASR.")
                review, mapped = validate_check(check, script, words["words"], measured["duration_seconds"],
                    self.settings.timing_tolerance_seconds, confirmed=True,
                    transcripts=(words["transcript"], confirmation["text"]))
                atomic_write_json(attempt_directory / f"check_{check_round}.json", review)
                break
            except AudioContractError as error:
                authority=text_authority(script,(words['transcript'],confirmation['text']),check)
                if authority['approved']:
                    self.manifest.update(text_verified=True,text_confirmation=authority,timing_status='invalid_alignment',fragments=[])
                    self.save()
                # Проверяем повторно только GPT-вердикт; WAV и обе ASR не меняются.
                atomic_write_json(attempt_directory / f"check_{check_round}_contract_error.json", {"reason": str(error), "review": check})
                if check_round == 2:
                    raise
                feedback = {"problem": str(error), "rejected_review": check,
                    "instruction": "Предыдущий JSON не прошёл локальную проверку. Проверь ожидаемые/наблюдаемые цитаты по исходным текстам ASR. Если observed_openai совпадает с expected после е/ё и пунктуации, нельзя утверждать, что оба ASR подтвердили ту же ошибку. Не выдумывай цитаты; observed_* должны буквально присутствовать в соответствующей расшифровке. Пересмотри только вердикт и привязку; это не основание создавать новый голос."}
        authority=text_authority(script,(words['transcript'],confirmation['text']),check)
        self.manifest.update(text_confirmation=authority,review=review, words=words["words"], transcript=words["transcript"],
                             text_verified=review["decision"] == "Можно использовать",
                             provisional_fragments=mapped if review["decision"] != "Можно использовать" else [])
        self.save()
        return review, mapped, words

    def run(self, rewrite=None, comparison_mode=False):
        try:
            self.current_attempt = None
            saved=self.directory/'audio_manifest.json'
            previous_manifest=read_json(saved) if saved.exists() else {}
            if previous_manifest.get('local_reassessment'):
                self.manifest=read_json(saved)
                from voice_local_recheck import reuse_local
                cached=reuse_local(self.directory,self.settings)
                if cached is not None:return cached
            while True:
                self.current_attempt = None
                script = approved_script(self.directory)
                script_id = script["script_version_id"]
                script_state = read_json(self.directory / "script_state.json")
                target = script_state["settings"]
                job_key = digest({"script_version_id": script_id, "settings_snapshot": self.snapshot})
                matches = [job for job in self.state["jobs"] if job["key"] == job_key]
                job = matches[-1] if matches else {"key": job_key, "script": script, "settings_snapshot": self.snapshot,
                    "script_revisions_at_start": script_state["revisions_used"], "attempts": []}
                if not matches:
                    self.state["jobs"].append(job)
                generation = self.state["regenerations_by_version"].get(script_id, 0)
                attempt_directory = self.directory / "voiceover" / f"{job_key[:16]}_{generation}"
                attempt_directory.mkdir(parents=True, exist_ok=True)
                self.state.update(status="running", reason=None, current_script_version_id=script_id)
                self.manifest = {"schema_version": 3, "mode": "real", "status": "running", "reason": None,
                    "script_version_id": script_id, "narration_text": script["narration_text"], "audio_kind": "ai_generated_speech",
                    "settings_snapshot": self.snapshot, "duration_source": "measured_wav_frames", "fragments": []}
                self.save()
                audio = self.synthesize(script, generation, attempt_directory)
                audio_result(self.directory, audio)
                if audio["audio_sha256"] in self.state["rejected_audio_hashes"]:
                    raise VoiceoverError("Получен ранее отклонённый WAV; он не принят как новая успешная попытка.")
                attempt = {"generation": generation, "script_version_id": script_id, "audio": audio,
                           "directory": str(attempt_directory.relative_to(self.directory)), "status": "checking"}
                known = next((entry for entry in job["attempts"] if entry["generation"] == generation), None)
                if known:
                    attempt = known
                else:
                    job["attempts"].append(attempt)
                self.current_attempt = attempt
                self.manifest.update(**audio, target_duration_seconds={"min": target["target_min_seconds"], "max": target["target_max_seconds"]})
                self.manifest["settings_snapshot"] = self.snapshot
                if previous_manifest.get('audio_sha256')==audio['audio_sha256'] and previous_manifest.get('script_version_id')==script_id:
                    for field in ['openai_transcript','transcript','review','text_verified','text_confirmation']:
                        if field in previous_manifest:self.manifest[field]=previous_manifest[field]
                    if previous_manifest.get('openai_transcript',{}).get('text'):
                        early=text_authority(script,(previous_manifest.get('transcript',''),previous_manifest['openai_transcript']['text']),previous_manifest.get('review'))
                        if early['approved']:self.manifest.update(text_verified=True,text_confirmation=early)
                self.save()
                current_path,_=audio_result(self.directory,audio)
                _,technical=current_audio_checks(current_path,audio.get('technical_checks'))
                self.manifest['technical_checks']=technical
                if not technical['passed']:
                    self.state['status']='technical_check_failed'
                    raise AudioTechnicalError('Актуальный WAV не прошёл техническую проверку: '+'; '.join(technical['issues'])+' Новый TTS не запускался.')
                review, mapped, words = self.check_audio(script, audio, attempt_directory)
                attempt.update(review=review, status=review["decision"])
                self.manifest.update(review=review, transcript=words["transcript"], words=words["words"],
                                     fragments=mapped if review["decision"] == "Можно использовать" else [])
                self.save()
                if review["decision"] == "Не уверен":
                    self.state["status"] = "needs_review"
                    raise VoiceoverError("Две расшифровки не позволили уверенно проверить текст/привязку. Нужен просмотр человеком; регенерации и сценарист не запускаются.")
                if review["decision"] == "Есть расхождения":
                    if comparison_mode:
                        self.state["status"] = "needs_review"
                        raise VoiceoverError("Подтверждена существенная ошибка текста. Сравнительная запись сохранена без повторной генерации.")
                    self.state["rejected_audio_hashes"].append(audio["audio_sha256"])
                    if generation >= self.settings.max_text_regenerations:
                        self.state["status"] = "needs_review"
                        raise VoiceoverError(f"Подтверждена ошибка чтения; лимит регенераций озвучки ({self.settings.max_text_regenerations}) исчерпан.")
                    self.state["regenerations_by_version"][script_id] = generation + 1
                    self.save()
                    continue
                duration = audio["duration_seconds"]
                if not target["target_min_seconds"] <= duration <= target["target_max_seconds"]:
                    if comparison_mode:
                        self.state["status"] = "duration_out_of_range"
                        raise VoiceoverError(f"Текст подтверждён, но WAV длится {duration:.3f} с вне {target['target_min_seconds']}–{target['target_max_seconds']} с. Сценарий сравнения не переписывался.")
                    feedback = {"measured_duration_seconds": duration, "script_version_id": script_id,
                                "audio_sha256": audio["audio_sha256"], "text_check": "Можно использовать",
                                "target_duration_seconds": {"min": target["target_min_seconds"], "max": target["target_max_seconds"]}}
                    if rewrite is None:
                        from pipeline import write_and_check
                        write_and_check(read_json(self.directory / "research.json"), self.directory,
                            read_json(self.directory / "run.json"), duration_feedback=feedback)
                    else:
                        rewrite(feedback)
                    if approved_script(self.directory)["script_version_id"] == script_id:
                        raise VoiceoverError("Сценарист не изменил версию после замечания о длительности; повторный цикл остановлен.")
                    # Новая версия заново проходит все операции; прежний бюджет сценариста сохранён.
                    continue
                self.state.update(status="completed", reason=None)
                self.manifest.update(status="completed", reason=None, fragments=mapped, text_verified=True,
                                     timing_status="aligned_to_deepgram_words", total_duration_seconds=duration)
                self.manifest["attempts"] = [{"script_version_id": item["script"]["script_version_id"], "attempts": item["attempts"]} for item in self.state["jobs"]]
                self.save()
                return self.directory / "audio_manifest.json"
        except Exception as error:
            reason = str(error) if isinstance(error, (VoiceoverError,)) or type(error).__name__ == "ResearchError" else "Технический сбой озвучки; дальнейшие этапы не запускались."
            status = "alignment_error" if isinstance(error,AudioAlignmentError) else "technical_check_failed" if isinstance(error,AudioTechnicalError) else "contract_error" if isinstance(error, AudioContractError) else self.state["status"] if self.state["status"] in {"needs_review", "duration_out_of_range", "technical_check_failed"} else "technical_error"
            self.state.update(status=status, reason=reason)
            self.manifest.update(status=status, reason=reason,
                text_verified=(getattr(error,'text_verified',False) or status in {'duration_out_of_range','technical_check_failed'} and self.manifest.get('text_verified',False)))
            self.manifest["attempts"] = [{"script_version_id": item["script"]["script_version_id"], "attempts": item["attempts"]} for item in self.state["jobs"]]
            if getattr(self, "current_attempt", None) is not None:
                self.current_attempt.update(status=status, reason=reason)
            self.save()
            raise VoiceoverError(reason + f" Результаты: {self.directory}") from None


def voiceover_from_file(path, demo=False, *, expected_script_version_id=None):
    if demo:
        raise VoiceoverError("ТЕСТ: новый блок озвучки проверяется подменой API в тестах; настоящая речь и таймкоды не создавались.")
    path = Path(path)
    directory = path if path.is_dir() else path.parent
    if path.is_file() and path.name == "scenes.json":
        load_verified_scenes(path)
    script=approved_script(directory)
    if expected_script_version_id is not None and script['script_version_id']!=expected_script_version_id:
        raise VoiceoverError('Approved script changed before parallel voice branch started; no API request sent.')
    keys, settings = load_voiceover_settings(require_keys=False)
    path = VoiceoverRun(directory, settings, keys).run()
    print(f"Полная озвучка и привязки: {path}", flush=True)
    return path


voiceover_from_script = voiceover_from_file
