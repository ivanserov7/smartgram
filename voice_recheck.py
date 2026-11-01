"""Повтор распознавания/проверки сохранённого WAV. Синтез и сценарист не вызываются."""

import argparse
from pathlib import Path

from file_io import atomic_write_json
from scene_files import read_json
from script_workflow import approved_script
from voice_errors import AudioContractError, AudioAlignmentError, AudioTechnicalError, VoiceoverError
from voice_audio_checks import current_audio_checks
from voice_settings import load_voiceover_settings
from voiceover import VoiceoverRun, audio_result


def recheck_saved(directory, new_round=False, *, api=None, keys=None, settings=None):
    directory = Path(directory)
    if settings is None or keys is None:
        keys, settings = load_voiceover_settings(require_keys=False)
    script = approved_script(directory)
    manifest = read_json(directory / "audio_manifest.json")
    if manifest.get("script_version_id") != script["script_version_id"]:
        raise VoiceoverError(
            "Текущий сценарий и сохранённый WAV относятся к разным версиям. Новый TTS не запускался."
        )
    if not new_round and manifest.get('local_reassessment'):
        from voice_local_recheck import reuse_local,reassess_saved
        try:
            cached=reuse_local(directory,settings)
            if cached is not None:return cached
        except VoiceoverError:
            reassess_saved(directory,settings)
            raise
    run = VoiceoverRun(directory, settings, keys, api=api)
    run.manifest = manifest
    rounds = run.state.setdefault("analysis_rounds", {})
    audio_hash = manifest["audio_sha256"]
    if new_round:
        if rounds.get(audio_hash, 0) >= 2:
            raise VoiceoverError(
                "Исчерпаны два явных дополнительных раунда ASR; бесконечных запросов нет."
            )
        rounds[audio_hash] = rounds.get(audio_hash, 0) + 1
    destination = (
        directory / "audio_checks" / f"{audio_hash[:16]}_{rounds.get(audio_hash,0)}"
    )
    destination.mkdir(parents=True, exist_ok=True)
    atomic_write_json(destination / "manifest_before.json", manifest)
    run.save()
    try:
        audio_path,measured=audio_result(directory,manifest)
        _,technical=current_audio_checks(audio_path,manifest.get('technical_checks'))
        run.manifest['technical_checks']=technical
        if not technical['passed']:
            raise AudioTechnicalError('Актуальный WAV технически непригоден: '+'; '.join(technical['issues'])+' Новый синтез не запускался.')
        review, mapped, words = run.check_audio(script, manifest, destination)
        target = read_json(directory / "script_state.json")["settings"]
        duration_ok = (
            target["target_min_seconds"]
            <= measured["duration_seconds"]
            <= target["target_max_seconds"]
        )
        status = (
            "completed"
            if review["decision"] == "Можно использовать" and duration_ok
            else (
                "duration_out_of_range"
                if review["decision"] == "Можно использовать"
                else "needs_review"
            )
        )
        reason = (
            None
            if status == "completed"
            else (
                "Длительность вне согласованного диапазона; сценарий не переписывался."
                if not duration_ok
                else review["decision"]
            )
        )
        run.manifest.update(
            status=status,
            reason=reason,
            fragments=mapped if review["decision"] == "Можно использовать" else [],
            provisional_fragments=(
                mapped if review["decision"] != "Можно использовать" else []
            ),
            text_verified=review["decision"] == "Можно использовать",
            total_duration_seconds=measured["duration_seconds"],
            timing_status='aligned_to_deepgram_words' if mapped and review['decision']=='Можно использовать' else 'invalid_alignment',
        )
        run.state.update(status=status, reason=reason)
    except VoiceoverError as error:
        status = (
            "alignment_error" if isinstance(error,AudioAlignmentError) else
            "technical_check_failed" if isinstance(error,AudioTechnicalError) else
            "contract_error" if isinstance(error, AudioContractError) else "technical_error"
        )
        run.state.update(status=status, reason=str(error))
        run.manifest.update(
            status=status, reason=str(error), text_verified=(getattr(error,'text_verified',False) or isinstance(error,AudioTechnicalError) and manifest.get('text_verified',False)), fragments=[]
        )
        run.save()
        raise
    run.save()
    return directory / "audio_manifest.json"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-directory", type=Path, required=True)
    parser.add_argument(
        "--new-asr-round",
        action="store_true",
        help="Явно разрешить новые платные ASR/GPT запросы, максимум два дополнительных раунда; WAV сохраняется.",
    )
    parser.add_argument('--local-only',action='store_true',help='Только сохранённые ASR/GPT и актуальный WAV; без любых API-запросов.')
    args = parser.parse_args()
    if args.local_only and args.new_asr_round:parser.error('--local-only несовместим с --new-asr-round')
    try:
        if args.local_only:
            from voice_local_recheck import reassess_saved
            print(reassess_saved(args.run_directory))
        else:print(recheck_saved(args.run_directory, args.new_asr_round))
    except (VoiceoverError, OSError, ValueError, KeyError) as error:
        parser.exit(
            1,
            (
                str(error)
                if isinstance(error, VoiceoverError)
                else "Не удалось прочитать сохранённый WAV/сценарий.\n"
            ),
        )
