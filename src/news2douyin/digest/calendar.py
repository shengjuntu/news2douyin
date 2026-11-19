"""Calendar bounds shared by live editions and historical replay."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo('Asia/Shanghai')


def days_before(day: date, count: int) -> date:
    return date.fromordinal(max(date.min.toordinal(), day.toordinal() - count))


def day_bounds_utc(day: date) -> tuple[datetime, datetime]:
    """Half-open UTC interval for this Beijing day, clipped to representable instants."""
    if day == date.min:
        start = datetime.min.replace(tzinfo=timezone.utc)
    else:
        start = datetime.combine(day, time.min, BEIJING).astimezone(timezone.utc)
    if day == date.max:
        end = datetime.max.replace(tzinfo=BEIJING).astimezone(timezone.utc) + timedelta(microseconds=1)
    else:
        end = datetime.combine(day + timedelta(days=1), time.min, BEIJING).astimezone(timezone.utc)
    return start, end
