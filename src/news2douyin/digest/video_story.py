"""Keep saved video stories usable as the basis for a new focus."""
from datetime import date, datetime
from urllib.parse import urlencode


def _day(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            return None
        if parsed.isoformat() == value:
            return parsed
    return None


def focus_url(card, *, day):
    """Open a new, unsaved Focus draft from this dated publication card."""
    selected = _day(day)
    key = card.get('key')
    if not selected or not isinstance(key, str) or not 1 <= len(key) <= 160:
        return ''
    return '/focus?' + urlencode({'new': 1, 'video_day': selected.isoformat(), 'video_key': key})
