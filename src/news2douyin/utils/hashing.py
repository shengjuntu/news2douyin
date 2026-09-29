from __future__ import annotations
import hashlib

def stable_hash(*parts: str, length: int = 24) -> str:
    s = "||".join([p or "" for p in parts]).encode("utf-8", errors="ignore")
    return hashlib.sha256(s).hexdigest()[:length]
