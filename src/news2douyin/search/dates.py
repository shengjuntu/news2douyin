"""Calendar-day filters, interpreted in the user's selected IANA timezone."""
from datetime import date, datetime, time, timedelta, timezone as utc_timezone
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def timezone_name(value=''):
    value = value or os.getenv('NEWS2DOUYIN_TIMEZONE', 'Asia/Shanghai')
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError('无效时区，请使用 Asia/Shanghai 或 UTC 等 IANA 时区')
    return value


def calendar_day(value='', timezone=''):
    zone = ZoneInfo(timezone_name(timezone))
    if not value:
        return datetime.now(zone).date().isoformat()
    if not isinstance(value, str):
        raise ValueError('日期格式必须为 YYYY-MM-DD 字符串')
    try:
        result = date.fromisoformat(value)
        if result.isoformat() != value:
            raise ValueError()
        return value
    except ValueError:
        raise ValueError('日期格式必须为 YYYY-MM-DD')


def date_range(period='all', date_from='', date_to='', timezone=''):
    zone = ZoneInfo(timezone_name(timezone))
    today = datetime.now(zone).date()
    if period == 'today':
        date_from = date_to = today.isoformat()
    elif period == 'yesterday':
        date_from = date_to = (today - timedelta(days=1)).isoformat()
    elif period in {'last7', 'last30'}:
        date_from = (today - timedelta(days=6 if period == 'last7' else 29)).isoformat()
        date_to = today.isoformat()
    elif period not in {'all', 'custom'}:
        raise ValueError('无效日期范围')
    # Explicit boundaries also work for older callers that omit period.
    if date_from:
        calendar_day(date_from, str(zone))
    if date_to:
        calendar_day(date_to, str(zone))
    if date_from and date_to and date_from > date_to:
        raise ValueError('开始日期不能晚于结束日期')
    def boundary(value, following=False):
        if not value:
            return None
        day = date.fromisoformat(value) + timedelta(days=int(following))
        return datetime.combine(day, time.min, zone).astimezone(utc_timezone.utc).isoformat()
    try:
        return boundary(date_from), boundary(date_to, True)
    except OverflowError:
        raise ValueError('日期超出支持范围')
