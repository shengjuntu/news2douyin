from openai import OpenAI, OpenAIError
from loguru import logger
import time
import random

from .configs.env import (
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    MODEL,
    USE_LLM_CACHE,
    LLM_CACHE_RATIO,
    LLM_CACHE_TTL_SEC,
)

from .cache_store import init_db, get_by_key, set_by_key, make_key_from_obj

# 初始化 sqlite 表（只做一次）
init_db()


class OpenaiClient:
    def __init__(self, opts):
        logger.debug("model:{} base_url:{}", MODEL, OPENAI_BASE_URL)
        self.client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)

        # Model + sampling params
        self.model_name = MODEL
        self.do_sample = opts.get("do_sample", False)
        self.seed = opts.get("seed", 0)
        self.temperature = opts.get("temperature", 0.2)
        self.max_tokens = opts.get("max_tokens", 900)
        self.top_p = opts.get("top_p", 1.0)
        self.presence_penalty = opts.get("presence_penalty", 0.0)
        self.frequency_penalty = opts.get("frequency_penalty", 0.0)
        self.best_of = opts.get("best_of", 1)
        self.timeout = opts.get("timeout", 60)

        # Retry config
        self.retry_count = opts.get("retry_count", 3)
        self.retry_delay = opts.get("retry_delay", 10)

        self.verbose = opts.get("verbose", False)
        for key, value in opts.items():
            logger.debug("{}:{}", key, value)

    def _make_cache_key(self, prompt_text: str, max_tokens: int) -> str:
        # 注意：把会影响输出的采样参数都纳入 key，避免错误复用
        payload = {
            "v": "chat_completions_v1",
            "model": self.model_name,
            "prompt": prompt_text,
            "seed": self.seed,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "presence_penalty": self.presence_penalty,
            "frequency_penalty": self.frequency_penalty,
            "max_tokens": max_tokens,
        }
        return make_key_from_obj(payload)

    def generate_messages(self, messages, max_tokens):
        param_max_tokens = self.max_tokens
        if max_tokens > 0:
            param_max_tokens = max_tokens

        completion = self.client.chat.completions.create(
            model=self.model_name,
            messages=messages,
            seed=self.seed,
            temperature=self.temperature,
            max_tokens=param_max_tokens,
            top_p=self.top_p,
            presence_penalty=self.presence_penalty,
            frequency_penalty=self.frequency_penalty,
            timeout=self.timeout,
        )
        return completion.choices[0].message.content

    def generate(self, prompt_text, max_tokens=-1):
        param_max_tokens = self.max_tokens if max_tokens <= 0 else max_tokens

        # --- 1) cache read -----------------------------------------------------
        cache_key = None
        if USE_LLM_CACHE:
            cache_key = self._make_cache_key(prompt_text, param_max_tokens)
            cached = get_by_key(cache_key, ttl_sec=LLM_CACHE_TTL_SEC)
            if cached is not None:
                # LLM_CACHE_RATIO < 1.0 可用于调试：抽样绕过缓存
                if float(LLM_CACHE_RATIO) >= 1.0 or random.random() < float(LLM_CACHE_RATIO):
                    logger.debug("LLM cache hit for prompt: {}", prompt_text[:80])
                    return cached
                logger.debug("LLM cache hit but bypassed (ratio={})", LLM_CACHE_RATIO)

        try:
            messages = [{"role": "user", "content": prompt_text}]
            if self.verbose:
                logger.debug("prompt_text:{}", prompt_text)

            response = self.generate_messages(messages, param_max_tokens)

            # --- 2) cache write ------------------------------------------------
            if USE_LLM_CACHE:
                if cache_key is None:
                    cache_key = self._make_cache_key(prompt_text, param_max_tokens)
                set_by_key(cache_key, response)

            return response

        except OpenAIError:
            logger.error("OpenAI API error/timeout. Prompt: {}", prompt_text[:80])
            return self.retry_generate(prompt_text, param_max_tokens)
        except Exception as e:
            logger.error("An error occurred: {}", e)
            return ""

    def retry_generate(self, prompt_text, max_tokens):
        retry_attempts = 0
        while retry_attempts < self.retry_count:
            logger.info("Retrying... Attempt {}/{}", retry_attempts + 1, self.retry_count)
            time.sleep(self.retry_delay)
            try:
                messages = [{"role": "user", "content": prompt_text}]
                response = self.generate_messages(messages, max_tokens)

                # retry 成功也写入缓存
                if USE_LLM_CACHE:
                    cache_key = self._make_cache_key(prompt_text, max_tokens)
                    set_by_key(cache_key, response)

                return response
            except OpenAIError:
                retry_attempts += 1
                logger.error("Retry attempt {} failed. Prompt: {}", retry_attempts, prompt_text[:80])
            except Exception as e:
                logger.error("An error occurred during retry: {}", e)
                return ""
        logger.error("All retry attempts failed. Prompt: {}", prompt_text[:80])
        return ""
