"""Настройки двух шагов сценариста. Секреты в снимок настроек не входят."""

from dataclasses import asdict, dataclass
import os
from pathlib import Path

from dotenv import dotenv_values

from research import ResearchError


@dataclass(frozen=True)
class ScriptSettings:
    writing_model: str = "gpt-6-luna"
    writing_reasoning_effort: str = "medium"
    writing_timeout_seconds: int = 120
    writing_max_retries: int = 0
    writing_prompt_file: str = "prompts/script_writing.txt"
    verification_model: str = "gpt-6-luna"
    verification_reasoning_effort: str = "medium"
    verification_timeout_seconds: int = 180
    verification_max_retries: int = 0
    verification_prompt_file: str = "prompts/script_verification.txt"
    verification_max_tool_calls: int = 8
    verification_context_size: str = "medium"
    target_min_seconds: int = 30
    target_max_seconds: int = 45
    max_revisions: int = 2

    def to_dict(self):
        return asdict(self)


def load_script_settings(env_path=None, require_key=True) -> tuple[str, ScriptSettings]:
    values = {**dotenv_values(env_path or Path(__file__).with_name(".env"), interpolate=False), **os.environ}
    key = (values.get("OPENAI_API_KEY") or "").strip()
    if require_key and (not key or key == "your_api_key_here"):
        raise ResearchError("Добавьте OPENAI_API_KEY в локальный .env для сценариста.")
    settings = {}
    for name, default in ScriptSettings().to_dict().items():
        env_name = "SCRIPT_" + name.upper()
        value = values.get(env_name, str(default))
        if isinstance(default, int):
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise ResearchError(f"{env_name} должен быть целым числом.") from None
            minimum = 0 if name.endswith("max_retries") or name == "max_revisions" else 1
            if value < minimum:
                raise ResearchError(f"Недопустимый {env_name}.")
        elif not isinstance(value, str) or not value.strip():
            raise ResearchError(f"Укажите {env_name}.")
        else:
            value = value.strip()
        settings[name] = value
    if settings["target_min_seconds"] > settings["target_max_seconds"]:
        raise ResearchError("SCRIPT_TARGET_MIN_SECONDS не может превышать SCRIPT_TARGET_MAX_SECONDS.")
    for name in ("writing_reasoning_effort", "verification_reasoning_effort"):
        if settings[name] not in {"none", "low", "medium", "high", "xhigh", "max"}:
            raise ResearchError(f"Недопустимый SCRIPT_{name.upper()}.")
    if settings["verification_context_size"] not in {"low", "medium", "high"}:
        raise ResearchError("Недопустимый SCRIPT_VERIFICATION_CONTEXT_SIZE.")
    return key, ScriptSettings(**settings)


def read_script_prompt(settings: ScriptSettings, step: str) -> str:
    path = Path(getattr(settings, step + "_prompt_file"))
    if not path.is_absolute():
        path = Path(__file__).parent / path
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise ResearchError(f"Не удалось прочитать промпт SCRIPT_{step.upper()}_PROMPT_FILE.") from None
    if not text.strip():
        raise ResearchError(f"Промпт SCRIPT_{step.upper()}_PROMPT_FILE пуст.")
    return text
