"""Раздельные оценки расходов озвучки; отсутствие тарифа/usage не означает ноль."""

from datetime import datetime, timezone
from pathlib import Path
import json


def summarize_voice_costs(directory):
    result = {
        name: {
            "requests": 0,
            "known_estimate_usd": 0.0,
            "unknown_attempts": 0,
            "confirmed_usd": None,
        }
        for name in ("gemini", "openai", "deepgram")
    }
    result["rates_checked_on"] = "2026-11-01"
    for path in Path(directory).glob("topic_*/gemini_usage.json"):
        for record in json.loads(path.read_text())["requests"]:
            for attempt in record["attempts"]:
                result["gemini"]["requests"] += 1
                usage = attempt.get("usage") or {}
                call_date = (
                    attempt.get("started_at_utc")
                    or datetime.now(timezone.utc).date().isoformat()
                )[:10]
                if (
                    record.get("request", {}).get("model") == "gemini-3.8-flash-tts"
                    and "2026-09-22" <= call_date <= "2026-12-31"
                    and usage.get("total_input_tokens") is not None
                    and usage.get("total_output_tokens") is not None
                ):
                    result["gemini"]["known_estimate_usd"] += (
                        usage["total_input_tokens"] * 0.5
                        + usage["total_output_tokens"] * 9
                    ) / 1_000_000
                else:
                    result["gemini"]["unknown_attempts"] += 1
    for path in Path(directory).glob("topic_*/openai_usage.json"):
        for record in json.loads(path.read_text())["requests"]:
            telemetry = record.get("telemetry", {})
            result["openai"]["requests"] += len(telemetry.get("attempts", [])) or 1
            usage = telemetry.get("usage") or {}
            if record.get("stage") == "audio_confirm" and "seconds" in usage:
                result["openai"]["known_estimate_usd"] += usage["seconds"] * 0.0045 / 60
            elif (
                telemetry.get("model") == "gpt-6-luna"
                and "input_tokens" in usage
                and "output_tokens" in usage
            ):
                details = usage.get("input_tokens_details") or {}
                cached, written = details.get("cached_tokens", 0), details.get(
                    "cache_write_tokens", 0
                )
                result["openai"]["known_estimate_usd"] += (
                    (usage["input_tokens"] - cached - written) * 0.1
                    + cached * 0.01
                    + written * 0.125
                    + usage["output_tokens"] * 0.5
                ) / 1_000_000
            else:
                result["openai"]["unknown_attempts"] += 1
    for path in Path(directory).glob("topic_*/deepgram_usage.json"):
        records = json.loads(path.read_text())["requests"]
        result["deepgram"]["requests"] += len(records)
        result["deepgram"]["unknown_attempts"] += len(records)
    result["notice"] = (
        "Оценки по платным тарифам и usage; это не подтверждённые списания. Free tier/скидки не угаданы. Deepgram не имеет настроенного тарифа. Gemini не учитывается через OpenAI Costs API."
    )
    result["deepgram"]["known_estimate_usd"] = None
    return result
