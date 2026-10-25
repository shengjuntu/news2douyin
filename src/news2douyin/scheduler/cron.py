"""Validated five-field schedules. Preserve the existing day AND weekday rule."""
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from ..search.dates import timezone_name


@lru_cache(maxsize=256)
def parse_cron(expression):
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError('Cron 必须为五段：分 时 日 月 星期')
    result = []
    for field, (minimum, maximum) in zip(fields, [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]):
        values = set()
        for part in field.split(','):
            if not part or part.count('/') > 1:
                raise ValueError('Cron 中有空项或无效步长')
            pieces = part.split('/')
            base = pieces[0]
            step = 1
            if len(pieces) == 2:
                if not pieces[1].isascii() or not pieces[1].isdigit():
                    raise ValueError('Cron 步长必须为正整数')
                step = int(pieces[1])
                if not 1 <= step <= maximum - minimum + 1:
                    raise ValueError('Cron 步长超出范围')
            if base == '*':
                start, end = minimum, maximum
            elif base.count('-') == 1:
                left, right = base.split('-')
                if not (left.isascii() and right.isascii() and left.isdigit() and right.isdigit()):
                    raise ValueError('Cron 范围必须为数字，例如 1-5')
                start, end = int(left), int(right)
            elif base.isascii() and base.isdigit():
                start = int(base)
                end = maximum if len(pieces) == 2 else start
            else:
                raise ValueError('Cron 仅支持数字、*、逗号、范围与步长')
            if not minimum <= start <= end <= maximum:
                raise ValueError(f'Cron 数值范围应为 {minimum}–{maximum}')
            values.update(range(start, end + 1, step))
        result.append(frozenset(values))
    result[4] = frozenset(value % 7 for value in result[4])
    return tuple(result)


def cron_matches(expression, moment):
    minute, hour, day, month, weekday = parse_cron(expression)
    return (moment.minute in minute and moment.hour in hour and moment.day in day and
            moment.month in month and (moment.weekday() + 1) % 7 in weekday)


def next_occurrences(expression, zone_name, *, after=None, count=3):
    zone = ZoneInfo(timezone_name(zone_name))
    after = after or datetime.now(timezone.utc)
    if after.tzinfo is None:
        raise ValueError('after must include timezone')
    after = after.astimezone(timezone.utc)
    minute, hour, days, months, weekdays = parse_cron(expression)
    start = after.astimezone(zone).date()
    output = []
    for delta in range(366 * 5):
        day = start + timedelta(days=delta)
        if day.day not in days or day.month not in months or (day.weekday() + 1) % 7 not in weekdays:
            continue
        instants = set()
        for h in hour:
            for m in minute:
                for fold in (0, 1):
                    local = datetime(day.year, day.month, day.day, h, m, tzinfo=zone, fold=fold)
                    instant = local.astimezone(timezone.utc)
                    # Round-trip excludes missing wall-clock times at DST start.
                    back = instant.astimezone(zone)
                    if back.replace(tzinfo=None) == local.replace(tzinfo=None) and instant > after:
                        instants.add(instant)
        for instant in sorted(instants):
            output.append(instant.astimezone(zone).isoformat(timespec='minutes'))
            if len(output) >= count:
                return output
    return output
