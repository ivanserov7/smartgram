"""Opt-in transport to a local VPN proxy, used only by Gemini TTS."""

import os
from pathlib import Path
from urllib.parse import urlsplit
from dotenv import dotenv_values

from voice_errors import VoiceoverError


def gemini_proxy_options(env_path=None):
    values = dotenv_values(env_path or Path(__file__).with_name(".env"), interpolate=False)
    value = (os.environ.get("GEMINI_HTTP_PROXY", values.get("GEMINI_HTTP_PROXY")) or "").strip()
    if not value:
        # Preserve the existing transport when the feature is not configured.
        return {}
    try:
        url = urlsplit(value)
        valid = (
            url.scheme == "http"
            and url.hostname in {"127.0.0.1", "::1"}
            and url.port is not None
            and 1 <= url.port <= 65535
            and url.username is None
            and url.password is None
            and url.path in {"", "/"}
            and not url.query
            and not url.fragment
            and not any(c.isspace() for c in value)
        )
    except ValueError:
        valid = False
    if not valid:
        # Never echo a mistaken subscription URL, UUID or proxy credentials.
        raise VoiceoverError(
            "GEMINI_HTTP_PROXY: нужен локальный HTTP-прокси вида "
            "http://127.0.0.1:17890 без пароля. VLESS/подписка настраивается "
            "в отдельном VPN-клиенте на сервере. Запрос Gemini не отправлен."
        ) from None
    # Explicit proxy ignores NO_PROXY and global proxies for this client only.
    # HTTPX raises on proxy failure; there is no fallback to a direct connection.
    return {"proxy": value, "trust_env": False}
