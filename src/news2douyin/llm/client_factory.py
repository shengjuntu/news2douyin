from __future__ import annotations
from typing import Protocol

class LLMClient(Protocol):
    def generate(self, prompt: str, max_tokens: int = 900) -> str: ...

def create_client_with_config(config_path: str) -> LLMClient:
    try:
        from .client import create_client_with_config as _cc  # type: ignore
        return _cc(config_path)
    except Exception as e:
        raise RuntimeError(
            "Missing LLM client. Provide `uwen` or edit this file to connect your LLM. "
            "Expected: client.generate(prompt, temperature, max_tokens) -> str"
        ) from e
