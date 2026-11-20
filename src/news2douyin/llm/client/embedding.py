"""Small OpenAI-compatible text embedding adapter for local or hosted services."""
from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
import json
from math import isfinite
import os
import sys
from urllib.parse import urlsplit

import requests


class EmbeddingError(RuntimeError):
    """The embedding service did not provide a usable result."""


class EmbeddingConfigurationError(EmbeddingError):
    """No unambiguous embedding endpoint or model is configured."""


class EmbeddingClient:
    def __init__(self, api_key: str, base_url: str, model: str, *,
                 dimensions: int | None = None, timeout: float = 30.0):
        self.base_url = base_url.strip().rstrip('/')
        if not self.base_url:
            raise EmbeddingConfigurationError('请设置 EMBEDDING_BASE_URL')
        try:
            endpoint = urlsplit(self.base_url)
            valid = (endpoint.scheme in ('http', 'https') and endpoint.hostname
                     and not endpoint.username and endpoint.password is None
                     and not endpoint.query and not endpoint.fragment
                     and not any(ch.isspace() for ch in self.base_url))
            endpoint.port
        except ValueError:
            valid = False
        if not valid:
            raise EmbeddingConfigurationError('EMBEDDING_BASE_URL 须为有效的 HTTP 或 HTTPS API 地址')
        if dimensions is not None and (type(dimensions) is not int or dimensions <= 0):
            raise EmbeddingConfigurationError('EMBEDDING_DIMENSIONS 须为正整数')
        if type(timeout) not in (int, float) or not isfinite(timeout) or timeout <= 0:
            raise EmbeddingConfigurationError('向量服务超时须为正数')
        self.model = model.strip()
        self.dimensions = dimensions
        self._timeout = (min(3.0, timeout), timeout)
        self._headers = {'Accept': 'application/json'}
        if api_key.strip():
            self._headers['Authorization'] = 'Bearer ' + api_key.strip()

    @classmethod
    def from_env(cls) -> EmbeddingClient:
        """Read process environment after the application has loaded its .env."""
        raw = os.getenv('EMBEDDING_DIMENSIONS', '').strip()
        try:
            dimensions = int(raw) if raw else None
        except ValueError:
            raise EmbeddingConfigurationError('EMBEDDING_DIMENSIONS 须为正整数') from None
        return cls(os.getenv('EMBEDDING_API_KEY', ''), os.getenv('EMBEDDING_BASE_URL', ''),
                   os.getenv('EMBEDDING_MODEL', ''), dimensions=dimensions)

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        try:
            response = requests.request(method, self.base_url + path, headers=self._headers,
                                        json=body, timeout=self._timeout, allow_redirects=False)
        except requests.Timeout:
            raise EmbeddingError('连接或等待向量服务响应超时') from None
        except requests.RequestException:
            raise EmbeddingError('无法连接向量服务') from None
        with response:
            if not 200 <= response.status_code < 300:
                raise EmbeddingError(f'向量服务返回 HTTP {response.status_code}')
            try:
                value = response.json()
            except ValueError:
                raise EmbeddingError('向量服务没有返回有效 JSON') from None
        if not isinstance(value, dict):
            raise EmbeddingError('向量服务响应格式无效')
        return value

    def list_models(self) -> list[str]:
        entries = self._request('GET', '/models').get('data')
        if not isinstance(entries, list):
            raise EmbeddingError('向量服务未返回模型列表')
        identifiers = [item.get('id') for item in entries if isinstance(item, dict)]
        return list(dict.fromkeys(value for value in identifiers if isinstance(value, str) and value.strip()))

    def resolve_model(self, *, check: bool = False) -> str:
        """Resolve the configured ID or discover the service's sole model."""
        if self.model and not check:
            return self.model
        models = self.list_models()
        if self.model:
            if self.model not in models:
                raise EmbeddingConfigurationError('EMBEDDING_MODEL 与 /v1/models 公布的 ID 不匹配')
        elif len(models) != 1:
            raise EmbeddingConfigurationError('无法自动确定唯一模型，请按 /v1/models 设置 EMBEDDING_MODEL')
        else:
            self.model = models[0]
        return self.model

    def get_embedding(self, text: str | Sequence[str]) -> list[list[float]]:
        if isinstance(text, str):
            texts = [text]
        elif isinstance(text, Sequence):
            texts = list(text)
        else:
            raise ValueError('请输入一段文本或文本列表')
        if not texts or any(not isinstance(item, str) or not item.strip() for item in texts):
            raise ValueError('待编码文本不能为空')
        rows = self._request('POST', '/embeddings', {
            'model': self.resolve_model(), 'input': texts, 'encoding_format': 'float',
        }).get('data')
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise EmbeddingError('返回向量数量与输入数量不符')
        vectors = {}
        dimensions = self.dimensions
        for row in rows:
            index = row.get('index') if isinstance(row, dict) else None
            if type(index) is not int or not 0 <= index < len(texts) or index in vectors:
                raise EmbeddingError('返回向量索引无效')
            embedding = row.get('embedding')
            if not isinstance(embedding, list) or not embedding or any(
                type(value) not in (float, int) for value in embedding
            ):
                raise EmbeddingError('服务未返回有效的数值向量')
            dimensions = dimensions or len(embedding)
            if len(embedding) != dimensions:
                raise EmbeddingError(f'向量维度不符，预期 {dimensions}，实际 {len(embedding)}')
            try:
                vector = [float(value) for value in embedding]
            except (OverflowError, ValueError):
                raise EmbeddingError('服务返回了非有限向量值') from None
            if not all(isfinite(value) for value in vector):
                raise EmbeddingError('服务返回了非有限向量值')
            vectors[index] = vector
        return [vectors[index] for index in range(len(texts))]

    async def get_embedding_async(self, text: str | Sequence[str]) -> list[list[float]]:
        return await asyncio.to_thread(self.get_embedding, text)


def main(argv=None) -> int:
    from ...config import load_environment

    parser = argparse.ArgumentParser(description='检查向量服务的模型和批量编码')
    parser.add_argument('--check', action='store_true', required=True, help='校验模型 ID 和实际返回向量')
    parser.parse_args(argv)
    load_environment()
    try:
        client = EmbeddingClient.from_env()
        model = client.resolve_model(check=True)
        vectors = client.get_embedding(['港口开始测试货运铁路', 'The port began testing a freight railway.'])
    except EmbeddingError as exc:
        print(json.dumps({'status': 'error', 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(dict(status='ok', model=model, dimensions=len(vectors[0]), batch_size=len(vectors)),
                     ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
