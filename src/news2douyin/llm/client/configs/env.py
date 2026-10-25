from ....config import load_environment
from pathlib import Path
import os

# 设置根目录
ROOT_PATH = "."
load_environment()

def _as_bool(x, default=False):
    if x is None:
        return default
    if isinstance(x, bool):
        return x
    s = str(x).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return default

def _as_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default

def _as_int(x, default=0):
    try:
        return int(float(x))
    except Exception:
        return default

config = {
    "EMBEDDING_BASE_URL": os.getenv("EMBEDDING_BASE_URL", "http://192.168.0.234:11435/v1"),
    "EMBEDDING_MODEL": os.getenv("EMBEDDING_MODEL", "bge-large-zh"),
    "EMBEDDING_API_KEY": os.getenv("EMBEDDING_API_KEY", "vllm"),
    "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY", "vllm"),
    "OPENAI_BASE_URL": os.getenv("OPENAI_BASE_URL", "http://192.168.0.234:9302/v1"),
    "MODEL": os.getenv("MODEL", "qwen72b"),

    # LLM cache
    "USE_LLM_CACHE": _as_bool(os.getenv("USE_LLM_CACHE", "true"), True),
    # 缓存命中后返回的概率（=1.0 表示稳定复用；<1.0 用于调试/抽样）
    "LLM_CACHE_RATIO": _as_float(os.getenv("LLM_CACHE_RATIO", "1.0"), 1.0),
    # 缓存数据库路径（sqlite）
    "LLM_CACHE_DB": os.getenv("LLM_CACHE_DB", "./runs/llm_cache/openai_cache.db"),
    # 缓存 TTL（秒），0 表示不过期
    "LLM_CACHE_TTL_SEC": _as_int(os.getenv("LLM_CACHE_TTL_SEC", "0"), 0),
}

EMBEDDING_BASE_URL = config["EMBEDDING_BASE_URL"]
EMBEDDING_MODEL = config["EMBEDDING_MODEL"]
EMBEDDING_API_KEY = config["EMBEDDING_API_KEY"]

OPENAI_API_KEY = config["OPENAI_API_KEY"]
OPENAI_BASE_URL = config["OPENAI_BASE_URL"]
MODEL = config["MODEL"]

USE_LLM_CACHE = config["USE_LLM_CACHE"]
LLM_CACHE_DB = config["LLM_CACHE_DB"]
LLM_CACHE_RATIO = config["LLM_CACHE_RATIO"]
LLM_CACHE_TTL_SEC = config["LLM_CACHE_TTL_SEC"]

print("EMBEDDING_BASE_URL:", EMBEDDING_BASE_URL)
print("EMBEDDING_MODEL:", EMBEDDING_MODEL)
print("OPENAI_BASE_URL:", OPENAI_BASE_URL)
print("MODEL:", MODEL)
print("USE_LLM_CACHE:", USE_LLM_CACHE)
print("LLM_CACHE_DB:", LLM_CACHE_DB)
print("LLM_CACHE_RATIO:", LLM_CACHE_RATIO)
print("LLM_CACHE_TTL_SEC:", LLM_CACHE_TTL_SEC)
