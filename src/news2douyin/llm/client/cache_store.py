import sqlite3
import json
import hashlib
import time
from pathlib import Path
from typing import Optional

from .configs.env import LLM_CACHE_DB, LLM_CACHE_TTL_SEC

DB_PATH = Path(LLM_CACHE_DB)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def _get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def init_db():
    with _get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cache(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                created REAL NOT NULL
            );
            """
        )


def make_key(prompt: str, model: str) -> str:
    """Legacy key: prompt + model fingerprint."""
    return hashlib.sha256(f"{model}::{prompt}".encode("utf-8")).hexdigest()


def make_key_from_obj(obj: dict) -> str:
    """Stable key from a JSON-serializable object."""
    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_expired(created_ts: float, ttl_sec: int) -> bool:
    if ttl_sec <= 0:
        return False
    return (time.time() - created_ts) > ttl_sec


def get_by_key(key: str, ttl_sec: Optional[int] = None) -> Optional[str]:
    ttl = int(LLM_CACHE_TTL_SEC if ttl_sec is None else ttl_sec)
    with _get_conn() as conn:
        row = conn.execute("SELECT value, created FROM cache WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        value_json, created_ts = row[0], float(row[1])
        if _is_expired(created_ts, ttl):
            # lazy delete expired entry
            try:
                conn.execute("DELETE FROM cache WHERE key=?", (key,))
            except Exception:
                pass
            return None
        try:
            return json.loads(value_json)["answer"]
        except Exception:
            return None


def set_by_key(key: str, answer: str):
    value = json.dumps({"answer": answer, "created": time.time()}, ensure_ascii=False)
    with _get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO cache(key,value,created) VALUES(?,?,?)",
            (key, value, time.time()),
        )


def get(prompt: str, model: str) -> Optional[str]:
    """Legacy API (kept): cache lookup by prompt+model."""
    return get_by_key(make_key(prompt, model))


def set(prompt: str, model: str, answer: str):
    """Legacy API (kept): cache write by prompt+model."""
    set_by_key(make_key(prompt, model), answer)
