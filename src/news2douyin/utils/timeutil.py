from __future__ import annotations
import datetime as _dt

def today_str(tz_name: str | None = None) -> str:
    # MVP: use local date. (If you want exact TZ conversion, add zoneinfo.)
    return _dt.date.today().isoformat()

def now_hhmm() -> str:
    return _dt.datetime.now().strftime("%H%M")

def utc_iso_now() -> str:
    return _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
