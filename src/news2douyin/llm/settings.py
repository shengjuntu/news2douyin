"""Shared runtime settings for V7's OpenAI-compatible HTTP operations."""
from dataclasses import dataclass
import os
import urllib.request


@dataclass(frozen=True)
class LLMSettings:
    base_url: str
    api_key: str
    model: str

    @property
    def headers(self) -> dict[str, str]:
        headers = {'Content-Type': 'application/json'}
        if self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}'
        return headers


def get_settings() -> LLMSettings:
    # Resolve at operation time, after any CLI/app factory loaded .env.
    return LLMSettings(
        os.getenv('OPENAI_BASE_URL', 'http://127.0.0.1:19993/v1').rstrip('/'),
        os.getenv('OPENAI_API_KEY', 'vllm'),
        os.getenv('MODEL', 'MiniCPM5-2B'),
    )


def probe_endpoint(timeout: float = 3.0) -> bool:
    settings = get_settings()
    request = urllib.request.Request(settings.base_url + '/models', headers=settings.headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except Exception:
        return False
