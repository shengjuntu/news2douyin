"""Bounded archive windows. Ranges are stored as UTC dates with an exclusive end."""
from datetime import date, timedelta
import re


_DAY = timedelta(days=1)
_DATE_HINT = re.compile(r'(?<!\d)((?:19|20)\d{2})(?:(?:年|[-/])(\d{1,2})(?:(?:月|[-/])(\d{1,2}))?)?(?!\d)')


def date_hints(queries, floor, end):
    """More precise date hints precede years, and the caller's boundaries always apply."""
    ranges = []
    for query in queries:
        for match in _DATE_HINT.finditer(query):
            year, month, day = match.groups()
            y, m = int(year), int(month or 1)
            try:
                start = date(y, m, int(day or 1))
                if day:
                    # Surround the named day: the archive date is the observation date.
                    lo, hi = start - timedelta(days=7), start + timedelta(days=8)
                elif month:
                    lo, hi = start, date(y + (m == 12), m % 12 + 1, 1)
                else:
                    lo, hi = start, date(y + 1, 1, 1)
            except ValueError:
                continue
            lo, hi = max(lo, floor), min(hi, end)
            if lo < hi:
                ranges.append((lo, hi))
    return sorted(dict.fromkeys(ranges), key=lambda pair: pair[1] - pair[0])


def merge_visited(ranges):
    result = []
    for lo, hi in sorted(ranges):
        if result and lo <= result[-1][1]:
            result[-1] = result[-1][0], max(result[-1][1], hi)
        else:
            result.append((lo, hi))
    return result


def next_window(ranges, visited, days):
    """Scan unvisited portions of the preferred ranges, then the general window."""
    for floor, stop in ranges:
        end = stop
        for lo, hi in reversed(visited):
            if lo >= end:
                continue
            if hi <= floor:
                break
            if hi < end:
                return max(floor, hi, end - timedelta(days=days)), end
            end = min(end, lo)
        if end > floor:
            return max(floor, end - timedelta(days=days)), end
    return None


def read_visited(state, floor, end):
    result = []
    for pair in state.get('visited', []):
        try:
            lo, hi = map(date.fromisoformat, pair)
        except (TypeError, ValueError):
            continue
        if floor <= lo < hi <= end:
            result.append((lo, hi))
    return merge_visited(result)
