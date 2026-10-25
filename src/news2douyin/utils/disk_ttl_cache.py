from __future__ import annotations
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

def _utc_ts() -> int:
    return int(time.time())

def _safe_makedirs(p: str) -> None:
    os.makedirs(p, exist_ok=True)

class FileLock:
    """
    Windows 友好的简易锁：用 O_EXCL 创建 lock 文件。
    - 成功创建：获得锁
    - 已存在：等待
    """
    def __init__(self, lock_path: str, timeout_sec: float = 30.0, poll_sec: float = 0.1):
        self.lock_path = lock_path
        self.timeout_sec = timeout_sec
        self.poll_sec = poll_sec
        self._fd: Optional[int] = None

    def __enter__(self):
        deadline = time.time() + self.timeout_sec
        while True:
            try:
                self._fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                # 写一点内容便于排查
                os.write(self._fd, str(os.getpid()).encode("utf-8", errors="ignore"))
                return self
            except FileExistsError:
                if time.time() >= deadline:
                    # 超时就放弃锁（不抛也行，看你偏好）
                    raise TimeoutError(f"FileLock timeout: {self.lock_path}")
                time.sleep(self.poll_sec)

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._fd is not None:
                os.close(self._fd)
        finally:
            self._fd = None
            try:
                os.remove(self.lock_path)
            except FileNotFoundError:
                pass

@dataclass
class CacheResult:
    hit: bool
    value: Any | None
    stale: bool = False
    path: str | None = None

class DiskTTLCache:
    def __init__(
        self,
        cache_dir: str,
        ttl_sec: int = 900,
        lock_timeout_sec: float = 30.0,
        allow_stale_if_error: bool = True,
    ):
        self.cache_dir = cache_dir
        self.ttl_sec = int(ttl_sec)
        self.lock_timeout_sec = float(lock_timeout_sec)
        self.allow_stale_if_error = bool(allow_stale_if_error)
        _safe_makedirs(self.cache_dir)

    def _paths(self, key: str) -> tuple[str, str, str]:
        base = os.path.join(self.cache_dir, key)
        return base + ".json", base + ".tmp", base + ".lock"

    def get(self, key: str) -> CacheResult:
        path, _, _ = self._paths(key)
        if not os.path.exists(path):
            return CacheResult(hit=False, value=None, path=path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
            created_at = int(obj.get("created_at", 0))
            ttl_sec = int(obj.get("ttl_sec", self.ttl_sec))
            now = _utc_ts()
            expired = (created_at + ttl_sec) < now
            return CacheResult(hit=not expired, value=obj.get("value"), stale=expired, path=path)
        except Exception:
            # 读坏了就当不存在
            return CacheResult(hit=False, value=None, path=path)

    def set(self, key: str, value: Any, ttl_sec: Optional[int] = None) -> str:
        path, tmp, _ = self._paths(key)
        obj = {"created_at": _utc_ts(), "ttl_sec": int(ttl_sec or self.ttl_sec), "value": value}
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)  # 原子替换（Windows 可用）
        return path

    def get_or_compute(self, key: str, compute: Callable[[], Any]) -> Any:
        # 1) 先无锁读：大多数情况直接命中
        r = self.get(key)
        if r.hit:
            return r.value

        # 2) 未命中/过期：加锁，防止击穿
        _, _, lock_path = self._paths(key)
        with FileLock(lock_path, timeout_sec=self.lock_timeout_sec):
            # 3) 再读一次（可能别的进程已写入）
            r2 = self.get(key)
            if r2.hit:
                return r2.value

            # 4) 真的去算/打 API
            try:
                v = compute()
                self.set(key, v)
                return v
            except Exception:
                # 5) 失败：可选返回 stale
                if self.allow_stale_if_error and r2.value is not None:
                    return r2.value
                # 再尝试返回第一次读到的 stale（如果有）
                if self.allow_stale_if_error and r.value is not None:
                    return r.value
                raise
