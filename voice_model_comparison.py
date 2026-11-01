"""Изолированные восемь вариантов, общий лимит $2, независимый ASR, без переозвучки."""

import argparse
import hashlib
import json
import os
import random
from decimal import Decimal
from pathlib import Path

from dotenv import dotenv_values

from comparison_audio import normalize_samples, save_audio
from file_io import atomic_write, atomic_write_json
from voice_comparison_api import ComparisonAPI, ComparisonError
from voice_errors import VoiceoverError
from voice_settings import load_voiceover_settings

PROJECT = Path(__file__).parent
VARIANTS = [
    {
        "id": "A",
        "model": "gpt-realtime-2.1-mini",
        "voice": "marin",
        "api": "realtime",
        "reserve": 0.11,
    },
    {
        "id": "B",
        "model": "gpt-realtime-2.1-mini",
        "voice": "cedar",
        "api": "realtime",
        "reserve": 0.11,
    },
    {
        "id": "C",
        "model": "gpt-realtime-2.1",
        "voice": "marin",
        "api": "realtime",
        "reserve": 0.30,
    },
    {
        "id": "D",
        "model": "gpt-realtime-2.1",
        "voice": "cedar",
        "api": "realtime",
        "reserve": 0.30,
    },
    {
        "id": "E",
        "model": "gpt-audio-1.5",
        "voice": "alloy",
        "api": "chat",
        "reserve": 0.30,
    },
    {
        "id": "F",
        "model": "gpt-4o-mini-tts",
        "voice": "marin",
        "api": "speech",
        "reserve": 0.15,
    },
    {"id": "G", "model": "tts-1-hd", "voice": "onyx", "api": "speech", "reserve": 0.03},
    {
        "id": "H",
        "model": "gpt-live-1",
        "voice": "marin",
        "api": "live",
        "reserve": 0.10,
    },
]


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def estimate(model, metadata, text=""):
    """Оценка из usage/тарифа, не списание. Отсутствующие данные остаются неизвестными."""
    usage = metadata.get("usage") or {}
    if model == "tts-1-hd":
        return len(text) * 30 / 1_000_000
    if model == "gpt-live-1":
        seconds = usage.get("seconds", metadata.get("session_open_seconds"))
        return seconds * 0.05 / 60 if seconds is not None else None
    if model == "gpt-transcribe":
        return usage["seconds"] * 0.0045 / 60 if "seconds" in usage else None
    input_total = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_total = usage.get("output_tokens", usage.get("completion_tokens"))
    if input_total is None or output_total is None:
        return None
    inputs = (
        usage.get("input_token_details")
        or usage.get("input_tokens_details")
        or usage.get("prompt_tokens_details")
        or {}
    )
    outputs = (
        usage.get("output_token_details")
        or usage.get("output_tokens_details")
        or usage.get("completion_tokens_details")
        or {}
    )
    if model == "gpt-6-luna":
        cached, written = inputs.get("cached_tokens", 0), inputs.get(
            "cache_write_tokens", 0
        )
        return (
            (input_total - cached - written) * 0.10
            + cached * 0.01
            + written * 0.125
            + output_total * 0.50
        ) / 1_000_000
    rates = {
        "gpt-realtime-2.1-mini": (0.6, 2.4, 20),
        "gpt-realtime-2.1": (4, 24, 64),
        "gpt-audio-1.5": (2.5, 10, 64),
        "gpt-4o-mini-tts": (0.6, 0, 12),
    }
    if model not in rates or "audio_tokens" not in outputs:
        return None
    input_text, output_text, output_audio = rates[model]
    # В эксперименте вход исключительно текстовый; аудиовхода у этих моделей нет.
    audio_tokens = outputs["audio_tokens"]
    return (
        input_total * input_text
        + (output_total - audio_tokens) * output_text
        + audio_tokens * output_audio
    ) / 1_000_000


class ComparisonRun:
    def __init__(self, root, api=None, key=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.text = (PROJECT / "comparison_text.txt").read_text().rstrip("\n")
        self.narrator = (
            (PROJECT / "prompts/comparison_narrator.txt").read_text().strip()
        )
        self.check_prompt = (
            (PROJECT / "prompts/comparison_check.txt").read_text().strip()
        )
        signature = {
            "text_sha256": sha(self.text),
            "narrator_sha256": sha(self.narrator),
            "check_prompt_sha256": sha(self.check_prompt),
            "variants": VARIANTS,
            "protocol_version": 1,
        }
        self.path = self.root / "comparison.json"
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
            if self.state["signature"] != signature:
                raise VoiceoverError(
                    "Конфигурация эксперимента изменилась; старый запуск не перезаписан. Нужна отдельная явно разрешённая партия."
                )
        else:
            order = list("ABCDEFGH")
            random.SystemRandom().shuffle(order)
            labels = {
                identifier: f"Образец {index+1}"
                for index, identifier in enumerate(order)
            }
            self.state = {
                "schema_version": 1,
                "mode": "real",
                "signature": signature,
                "budget_limit_usd": 2,
                "preflight": {
                    "estimate_usd": 0.65,
                    "assumptions": "Планирование: около 40 с на вариант, 50 выходных аудиотокенов/с как предположение; это не измеренный usage. Технические резервы отдельно.",
                    "all_first_attempt_reservations_usd": round(
                        sum(v["reserve"] for v in VARIANTS) + 0.16, 6
                    ),
                    "pricing_source": "https://developers.openai.com/api/docs/pricing",
                    "pricing_checked_on": "2026-11-01",
                },
                "blind_order": order,
                "entries": [
                    {
                        **variant,
                        "blind_label": labels[variant["id"]],
                        "status": "not_started",
                    }
                    for variant in VARIANTS
                ],
                "operations": {},
                "confirmed_experiment_cost_usd": None,
                "confirmed_cost_reason": "Точная привязка списаний этого эксперимента ожидает Costs API; оценка не является списанием.",
            }
        values = {**dotenv_values(PROJECT / ".env", interpolate=False), **os.environ}
        self.key = key or values.get("OPENAI_API_KEY") or ""
        _, settings = load_voiceover_settings(require_keys=False)
        self.api = api or ComparisonAPI(
            self.root, self.key, settings, self.text, self.narrator, self.check_prompt
        )
        atomic_write(self.root / "source_text.txt", self.text.encode())
        self.save()

    def save(self):
        atomic_write_json(self.path, self.state)

    def committed(self):
        return sum(
            (
                Decimal(
                    str(
                        attempt.get("estimated_usd")
                        if attempt.get("estimated_usd") is not None
                        else attempt["reserved_usd"]
                    )
                )
                for op in self.state["operations"].values()
                for attempt in op["attempts"]
            ),
            Decimal(0),
        )

    def operation(self, identity, stage, reserve, function, model):
        existing = self.state["operations"].get(identity)
        if existing:
            if existing["status"] == "completed":
                return existing["result"]
            raise ComparisonError(
                existing.get("error")
                or "Исход прежней операции неизвестен; после перезапуска новый запрос не отправлен.",
                status=existing.get("error_status", "error"),
            )
        if not self.key:
            raise ComparisonError(
                "Нет OPENAI_API_KEY в .env; запрос не отправлен.", status="unavailable"
            )
        operation = {
            "stage": stage,
            "model": model,
            "status": "started",
            "attempts": [],
        }
        self.state["operations"][identity] = operation
        self.save()
        for number in (1, 2):
            if self.committed() + Decimal(str(reserve)) > Decimal("2"):
                operation.update(
                    status="failed",
                    error="Лимит $2: новый запрос не отправлен.",
                    error_status="budget_blocked",
                )
                self.save()
                raise ComparisonError(operation["error"], status="budget_blocked")
            attempt = {
                "number": number,
                "status": "started",
                "reserved_usd": reserve,
                "estimated_usd": None,
            }
            operation["attempts"].append(attempt)
            self.save()
            try:
                result = function(number)
                metadata = result.get("metadata", {})
                attempt.update(
                    status="completed",
                    estimated_usd=estimate(model, metadata, self.text),
                )
                operation.update(status="completed", result=result)
                self.save()
                return result
            except Exception as error:
                message = (
                    str(error)
                    if isinstance(error, VoiceoverError)
                    else f"{type(error).__name__}: непригодный ответ/файл, результат не объявлен успешным."
                )
                status = getattr(error, "status", "error")
                attempt.update(
                    status="failed",
                    error=message,
                    error_details=getattr(error, "details", None),
                )
                operation.update(status="failed", error=message, error_status=status)
                self.save()
                if (
                    not isinstance(error, ComparisonError)
                    or not error.technical
                    or number == 2
                ):
                    raise ComparisonError(
                        message, status=status, details=getattr(error, "details", None)
                    ) from None
                # Только одна отдельная техническая попытка, каждая заново резервирует деньги.

    def generate(self, entry):
        directory = self.root / entry["id"]
        directory.mkdir(exist_ok=True)
        atomic_write(directory / "source_text.txt", self.text.encode())
        atomic_write_json(
            directory / "input.json",
            {
                "text": self.text,
                "text_sha256": sha(self.text),
                "narrator_instruction": self.narrator,
                "instructions_supported": entry["model"] != "tts-1-hd",
                "variant": {k: entry[k] for k in ("id", "model", "voice", "api")},
            },
        )

        def generate(number):
            attempt = directory / f"generation_{number}"
            attempt.mkdir(exist_ok=True)
            content, metadata = self.api.generate(entry, attempt)
            audio = save_audio(attempt, content)
            return {
                "metadata": metadata,
                "audio": audio,
                "attempt_directory": str(attempt.relative_to(self.root)),
            }

        result = self.operation(
            "generation:" + entry["id"],
            "generation",
            entry["reserve"],
            generate,
            entry["model"],
        )
        source = self.root / result["attempt_directory"] / "original.wav"
        if (
            not source.is_file()
            or hashlib.sha256(source.read_bytes()).hexdigest()
            != result["audio"]["audio_sha256"]
        ):
            raise ComparisonError(
                "Сохранённый оплаченный WAV отсутствует или изменился; генерация не повторяется."
            )
        # Стабильные имена для плеера, исходный ответ остаётся в папке попытки.
        atomic_write(directory / "original.wav", source.read_bytes())
        entry.update(
            status="generated", audio=result["audio"], generation=result["metadata"]
        )
        self.save()

    def review(self, entry):
        directory = self.root / entry["id"]
        path = directory / "original.wav"
        audio_hash = entry["audio"]["audio_sha256"]

        def transcribe(number):
            attempt = directory / f"transcription_{number}"
            attempt.mkdir(exist_ok=True)
            raw, metadata = self.api.transcribe(path, attempt)
            if not isinstance(raw.get("text"), str) or not raw["text"].strip():
                raise ComparisonError(
                    "Независимый ASR вернул пустой текст; образец не перегенерирован."
                )
            return {"metadata": metadata, "transcription": raw}

        # Кеш по хешу WAV, модели и параметрам; один и тот же звук не оплачиваем дважды.
        transcription = self.operation(
            "transcription:" + audio_hash + ":gpt-transcribe:ru",
            "transcription",
            0.01,
            transcribe,
            "gpt-transcribe",
        )
        entry["transcription"] = transcription["transcription"]
        atomic_write_json(directory / "transcription.json", transcription)

        def check(number):
            attempt = directory / f"check_{number}"
            attempt.mkdir(exist_ok=True)
            review, metadata = self.api.check(
                transcription["transcription"]["text"], attempt
            )
            return {"review": review, "metadata": metadata}

        review = self.operation(
            "check:"
            + sha(
                json.dumps(
                    [
                        audio_hash,
                        entry["transcription"],
                        self.text,
                        self.check_prompt,
                        "gpt-6-luna",
                        "low",
                    ],
                    ensure_ascii=False,
                    sort_keys=True,
                )
            ),
            "text_check",
            0.01,
            check,
            "gpt-6-luna",
        )
        entry.update(review=review["review"], status="ready_for_listening")
        if not review["review"]["full_text_present"]:
            entry["status"] = "text_completeness_uncertain"
        atomic_write_json(directory / "text_review.json", review)
        self.save()

    def run(self):
        from voice_comparison_report import build_report

        for entry in self.state["entries"]:
            print(entry["id"], entry["model"], entry["voice"], flush=True)
            try:
                self.generate(entry)
                self.review(entry)
            except VoiceoverError as error:
                entry.update(status=getattr(error, "status", "error"), error=str(error))
                self.save()
                print("  " + entry["status"] + ": " + str(error)[:1000], flush=True)
            normalize_samples(self.state["entries"], self.root)
            self.state["budget_committed_usd"] = float(self.committed())
            self.save()
            build_report(self.root, self.state, self.text)
        return self.root / "report.html"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--directory",
        type=Path,
        help="Папка отдельного опыта; повторный запуск переиспользует его результаты.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать варианты/резервы без API и создания аудио.",
    )
    args = parser.parse_args()
    if args.dry_run:
        print(
            json.dumps(
                {
                    "variants": VARIANTS,
                    "budget_limit_usd": 2,
                    "first_attempt_reservations_usd": round(
                        sum(v["reserve"] for v in VARIANTS) + 0.16, 6
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    root = args.directory
    pointer = PROJECT / "outputs/voice_model_comparison_latest.json"
    try:
        if root is None:
            (PROJECT / "outputs").mkdir(exist_ok=True)
            if pointer.exists():
                saved = json.loads(pointer.read_text())
                if saved.get("schema_version") != 1 or not isinstance(
                    saved.get("directory"), str
                ):
                    raise VoiceoverError(
                        "Повреждён указатель эксперимента; новый платный запуск не создан."
                    )
                root = Path(saved["directory"])
                if not (root / "comparison.json").is_file():
                    raise VoiceoverError(
                        "Указатель ведёт в отсутствующий опыт; новый платный запуск не создан."
                    )
            else:
                root = PROJECT / "outputs/voice_model_comparison"
        run = ComparisonRun(root)
        pointer.parent.mkdir(exist_ok=True)
        atomic_write_json(
            pointer, {"schema_version": 1, "directory": str(root.resolve())}
        )
        report = run.run()
        print("Отчёт:", report)
    except (VoiceoverError, OSError, ValueError) as error:
        print("Эксперимент остановлен:", str(error))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
