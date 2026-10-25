from __future__ import annotations
from typing import Protocol

class LLMClient(Protocol):
    def generate(self, prompt: str, max_tokens: int = 900) -> str: ...

def create_client_with_config(config_path: str) -> LLMClient:
    try:
        from .client import create_client_with_config as _cc  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            'Legacy LLM dependencies are missing. Install: pip install "news2douyin[llm]"'
        ) from e
    return _cc(config_path)
