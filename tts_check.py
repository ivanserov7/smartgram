"""Короткий WAV выбранного синтезатора; не проверенный производственный результат."""

import argparse
import sys
from pathlib import Path

from audio_files import inspect_wav
from file_io import atomic_write, atomic_write_json
from voice_factory import create_voice_api
from voice_errors import VoiceoverError
from voice_settings import load_voiceover_settings
from voiceover import digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", default="Привет! Это проверка русского голоса Smartgram.")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/tts_check"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.text.strip() or len(args.text) > 300:
        parser.error("Нужен непустой текст до 300 символов.")
    if args.dry_run:
        print("Dry-run: один диагностический WAV выбранного провайдера; Deepgram/исследование не запускаются.")
        return 0
    try:
        keys, settings = load_voiceover_settings(require_keys=False)
        required = "GEMINI_API_KEY" if settings.tts_provider == "gemini" else "OPENAI_API_KEY"
        if not keys.get(required):
            raise VoiceoverError(f"Для образца нужен {required} в локальном .env; другие API не запускаются.")
        args.output_dir.mkdir(parents=True, exist_ok=True)
        if (args.output_dir / "audio.wav").exists():
            raise VoiceoverError("Диагностический WAV уже существует. Выберите новую папку явно, чтобы не повторять платный вызов незаметно.")
        script = {"narration_text": args.text, "script_version_id": "sample_" + digest(args.text)}
        content, metadata = create_voice_api(args.output_dir, settings, keys).synthesize(script, args.output_dir)
        atomic_write(args.output_dir / "audio.wav", content)
        atomic_write_json(args.output_dir / "audio_manifest.json", {**metadata, **inspect_wav(content), "purpose": "voice_check",
            "status": "unverified", "reason": "Только диагностический образец. Текст/привязки не проверялись.", "narration_text": args.text})
        print(f"Диагностический WAV: {args.output_dir / 'audio.wav'}. Голос создан ИИ; текст не проверялся.")
        return 0
    except (OSError, VoiceoverError) as error:
        print(str(error) if isinstance(error, VoiceoverError) else "Ошибка сохранения диагностического WAV.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
