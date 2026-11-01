"""Отдельные настройки двух этапов исследователя; OPENAI_MODEL остальных этапов не меняется."""

from dataclasses import asdict, dataclass
from pathlib import Path

from dotenv import dotenv_values

from research import ResearchError


@dataclass(frozen=True)
class ResearchSettings:
    search_model: str = "gpt-6-luna"
    search_reasoning_effort: str = "medium"
    search_context_size: str = "medium"
    max_tool_calls_per_round: int = 8
    search_timeout_seconds: int = 180
    assessment_model: str = "gpt-6-luna"
    assessment_reasoning_effort: str = "low"
    assessment_timeout_seconds: int = 120
    max_rounds: int = 2

    def to_dict(self):
        return asdict(self)


def load_research_settings() -> tuple[str, ResearchSettings]:
    values = dotenv_values(Path(__file__).with_name(".env"), interpolate=False)
    key = (values.get("OPENAI_API_KEY") or "").strip()
    if not key or key == "your_api_key_here":
        raise ResearchError("Добавьте OPENAI_API_KEY в .env.")
    defaults = ResearchSettings().to_dict()
    settings = {}
    for name, default in defaults.items():
        env_name = "RESEARCH_" + name.upper()
        value = values.get(env_name, str(default))
        if isinstance(default, int):
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise ResearchError(f"{env_name} должен быть целым числом.") from None
            ceiling = {"max_rounds": 2, "max_tool_calls_per_round": 8}.get(name)
            if value < 1 or ceiling is not None and value > ceiling:
                raise ResearchError(f"Недопустимый {env_name}.")
        else:
            if not isinstance(value, str) or not value.strip():
                raise ResearchError(f"Укажите {env_name}.")
            value = value.strip()
        settings[name] = value
    for name in ("search_reasoning_effort", "assessment_reasoning_effort"):
        if settings[name] not in {"none", "low", "medium", "high", "xhigh", "max"}:
            raise ResearchError(f"Недопустимый RESEARCH_{name.upper()}.")
    if settings["search_context_size"] not in {"low", "medium", "high"}:
        raise ResearchError("Недопустимый RESEARCH_SEARCH_CONTEXT_SIZE.")
    return key, ResearchSettings(**settings)
