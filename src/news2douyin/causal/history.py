"""On-demand GDELT archive leads and original publisher excerpts for a tree step."""
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import os
import re
from time import monotonic
import unicodedata
from urllib.parse import urlsplit

from ..dedup.normalize import STOPWORDS, canonicalize_url
from ..desk.rules import ALIASES
from ..research.sources import safe_fetch
from ..selection.source_content import source_url
from ..selection.source_fetch import _decode, _description, _headline, _PreviewReader
from .chronology import interval

PROJECT_ENV = 'NEWS2DOUYIN_CAUSAL_HISTORY_BIGQUERY_PROJECT'
BYTES_ENV = 'NEWS2DOUYIN_CAUSAL_HISTORY_MAX_BYTES_BILLED'
DAYS_ENV = 'NEWS2DOUYIN_CAUSAL_HISTORY_LOOKBACK_DAYS'
ARCHIVE_START = date(2015, 2, 19)
DEFAULT_LOOKBACK = 90
DEFAULT_BYTES_BILLED = 12 * 1024 ** 3
MAX_LEADS = 48
MAX_FETCHES = 12
FETCH_SECONDS = 6
PUBLISHER_BUDGET_SECONDS = 48
MAX_DOCUMENT_BYTES = 2 * 1024 ** 2
QUERY_SECONDS = 65
_WORD = re.compile(r'[a-z][a-z0-9]{2,}|[\u3400-\u9fff]{2,12}')
_GENERIC = frozenset(('possible', 'potential', 'cause', 'before', 'after', 'event',
                      'mechanism', 'possible cause', 'report', 'history', 'timeline'))
_PUBLISHER_MARKERS = ('article:published_time', 'datepublished', 'parsely-pub-date',
                      'sailthru.date', 'citation_publication_date')
_ARTICLE_TYPES = frozenset(('Article', 'NewsArticle', 'ReportageNewsArticle',
                           'AnalysisNewsArticle', 'BackgroundNewsArticle',
                           'OpinionNewsArticle', 'ReviewNewsArticle', 'BlogPosting',
                           'LiveBlogPosting', 'SocialMediaPosting'))


# Both public tables are ingestion-time partitioned. GDELT times select leads
# only; they are not publisher dates or evidence of an event's actual time.
_SQL = """WITH events AS (
  SELECT GlobalEventID AS event_id,
    REGEXP_REPLACE(LOWER(CONCAT(COALESCE(Actor1Name, ''), ' ',
      COALESCE(Actor2Name, ''), ' ', COALESCE(Actor1CountryCode, ''), ' ',
      COALESCE(Actor2CountryCode, ''), ' ', COALESCE(ActionGeo_FullName, ''), ' ',
      COALESCE(Actor1Geo_FullName, ''), ' ', COALESCE(Actor2Geo_FullName, ''), ' ',
      COALESCE(SOURCEURL, ''))), r'[-_/]+', ' ') AS terms
  FROM `gdelt-bq.gdeltv2.events_partitioned`
  WHERE _PARTITIONTIME >= TIMESTAMP(@date_from)
    AND _PARTITIONTIME < TIMESTAMP(@date_end)
    AND SQLDATE >= @event_from AND SQLDATE < @event_end
), mentions AS (
  SELECT GlobalEventID AS event_id, MentionIdentifier AS url,
    COALESCE(Confidence, 0) AS confidence, MentionTimeDate AS mention_time
  FROM `gdelt-bq.gdeltv2.eventmentions_partitioned`
  WHERE _PARTITIONTIME >= TIMESTAMP(@date_from)
    AND _PARTITIONTIME < TIMESTAMP(@date_end)
    AND MentionType = 1
    AND (STARTS_WITH(MentionIdentifier, 'https://') OR STARTS_WITH(MentionIdentifier, 'http://'))
), ranked AS (
  SELECT m.*,
    (SELECT COUNT(*) FROM UNNEST(@keywords) AS keyword WHERE STRPOS(e.terms, keyword) > 0)
    + 2 * (SELECT COUNT(*) FROM UNNEST(@keywords) AS keyword
      WHERE STRPOS(REGEXP_REPLACE(LOWER(m.url), r'[-_/]+', ' '), keyword) > 0) AS relevance
  FROM mentions AS m JOIN events AS e USING (event_id)
), unique_urls AS (
  SELECT * FROM ranked WHERE relevance > 0
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY url ORDER BY relevance DESC, confidence DESC, mention_time DESC, event_id
  ) = 1
), per_event AS (
  SELECT * FROM unique_urls
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY event_id ORDER BY relevance DESC, confidence DESC, mention_time DESC, url
  ) <= 2
)
SELECT event_id, url FROM per_event
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY COALESCE(NET.REG_DOMAIN(url), NET.HOST(url), url)
  ORDER BY relevance DESC, confidence DESC, mention_time DESC, event_id, url
) <= 4
ORDER BY relevance DESC, confidence DESC, mention_time DESC, event_id, url
LIMIT @lead_limit"""


@dataclass
class HistoryResult:
    candidates: list[dict] = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)


class _HistoryError(RuntimeError):
    pass


def _setting(name, default, maximum):
    raw = os.getenv(name, '').strip()
    if not raw:
        return default
    try:
        value = int(raw)
        if 0 < value <= maximum:
            return value
    except ValueError:
        pass
    raise _HistoryError('invalid_configuration')


def _utc(value):
    point = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return point.astimezone(timezone.utc) if point.tzinfo else point.replace(tzinfo=timezone.utc)


def _keywords(queries, event_text):
    values = []
    for value in (*queries[:3], event_text[:500]):
        phrase = unicodedata.normalize('NFKC', value).casefold()[:500]
        aliases = [term for name, terms in ALIASES.items() if name in phrase for term in terms]
        # The proposal can append multilingual anchor words. Preserve short
        # comma-delimited phrases and recognize their hyphenated URL forms.
        parts = [part.strip() for part in re.split(r'[,，、;；|：:]|[()]', phrase)]
        phrases = [part for part in parts if 1 < len(part.split()) <= 5 and len(part) <= 55]
        words = _WORD.findall(phrase)
        terms = (term for term in (*phrases, *(word for word in words if word.isascii()), *aliases, *words)
                 if term not in STOPWORDS and term not in _GENERIC)
        values.extend(list(dict.fromkeys(terms))[:14])
    return list(dict.fromkeys(values))[:40]


def _query_error(error):
    reasons = {item.get('reason') for item in getattr(error, 'errors', ()) or ()
               if isinstance(item, dict)}
    if 'bytesBilledLimitExceeded' in reasons or 'exceeded limit for bytes billed' in str(error).casefold():
        return 'bytes_limit'
    if 'stopped' in reasons or 'timeout' in reasons or isinstance(error, TimeoutError):
        return 'query_timeout'
    if 'rateLimitExceeded' in reasons or 'quotaExceeded' in reasons:
        return 'query_quota'
    if type(error).__name__ in ('DefaultCredentialsError', 'RefreshError', 'Forbidden', 'Unauthorized'):
        return 'credentials_or_access'
    return 'query_failed'


def _run_query(project, start, end, keywords, max_bytes):
    try:
        from google.cloud import bigquery
    except ImportError:
        raise _HistoryError('missing_dependency') from None
    client, job = None, None
    try:
        client = bigquery.Client(project=project, location='US')
        config = bigquery.QueryJobConfig(
            use_legacy_sql=False, maximum_bytes_billed=max_bytes,
            job_timeout_ms=QUERY_SECONDS * 1000,
            query_parameters=[
                bigquery.ScalarQueryParameter('date_from', 'DATE', start),
                bigquery.ScalarQueryParameter('date_end', 'DATE', end),
                bigquery.ScalarQueryParameter('event_from', 'INT64', int(start.strftime('%Y%m%d'))),
                bigquery.ScalarQueryParameter('event_end', 'INT64', int(end.strftime('%Y%m%d'))),
                bigquery.ArrayQueryParameter('keywords', 'STRING', keywords),
                bigquery.ScalarQueryParameter('lead_limit', 'INT64', MAX_LEADS),
            ],
        )
        job = client.query(_SQL, job_config=config, retry=None, job_retry=None, timeout=12)
        rows = [dict(row) for row in job.result(max_results=MAX_LEADS, page_size=MAX_LEADS,
                                               retry=None, job_retry=None, timeout=QUERY_SECONDS)]
        return rows, job.total_bytes_billed or 0
    except Exception as exc:
        code = _query_error(exc)
        if job is not None and code in ('query_timeout', 'query_failed'):
            try:
                job.cancel(retry=None, timeout=5)
            except Exception:
                pass
        raise _HistoryError(code) from None
    finally:
        if client is not None:
            client.close()


class _PublisherReader(_PreviewReader):
    """Record explicit publication signals while reusing visible paragraphs."""

    def __init__(self):
        super().__init__()
        self.explicit_dates = []
        self.structured = []
        self._script = None
        self._script_chars = 0

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        attrs = dict(attrs)
        marker = (attrs.get('property') or attrs.get('name') or attrs.get('itemprop') or '').lower()
        if len(self.explicit_dates) < 24:
            if tag == 'meta' and marker in _PUBLISHER_MARKERS:
                self.explicit_dates.append(attrs.get('content'))
            elif tag == 'time' and marker == 'datepublished' and self.article_depth and not self._skipped():
                self.explicit_dates.append(attrs.get('datetime'))
        if tag == 'script' and (attrs.get('type') or '').lower() == 'application/ld+json' and len(self.structured) < 12:
            self._script, self._script_chars = [], 0

    def handle_data(self, data):
        if self._script is not None:
            part = data[:max(0, 80_000 - self._script_chars)]
            self._script.append(part)
            self._script_chars += len(part)
        super().handle_data(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self._script is not None:
            self.structured.append(''.join(self._script))
            self._script = None
        super().handle_endtag(tag)

    def date_literals(self, headline):
        yield from self.explicit_dates
        entries = []
        for payload in self.structured:
            try:
                node = json.loads(payload)
            except (ValueError, RecursionError):
                continue
            pending = list(node[:30]) if isinstance(node, list) else [node]
            for item in pending:
                if not isinstance(item, dict):
                    continue
                graph = item.get('@graph')
                if isinstance(graph, list) and len(pending) < 60:
                    pending.extend(graph[:20])
                types = item.get('@type')
                types = types[:10] if isinstance(types, list) else [types]
                if any(isinstance(kind, str) and kind.rsplit('/', 1)[-1] in _ARTICLE_TYPES for kind in types):
                    entries.append(item)
        matching = {item['datePublished'] for item in entries if isinstance(item.get('datePublished'), str)
                    and ((_headline(item.get('headline'), self.meta.get('og:site_name', '')).casefold()
                          == headline.casefold() and headline)
                         or (len(entries) == 1 and not item.get('headline')))}
        yield from matching


def _publication(raw):
    if not isinstance(raw, str) or not re.match(r'^\d{4}-\d{2}-\d{2}(?:[T ]|$)', raw.strip()):
        return ''
    try:
        point = datetime.fromisoformat(raw.strip().replace('Z', '+00:00'))
        if point.tzinfo is None:
            return point.date().isoformat()
        return point.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    except (ValueError, OverflowError):
        return ''


def _consistent_publication(literals):
    # An exact timestamp and a date-only marker may describe the same local
    # day. Explicit markers that disagree stay unresolved.
    exact = {}
    dates = set()
    for raw in literals:
        if stamp := _publication(raw):
            if len(stamp) == 10:
                dates.add(stamp)
            else:
                exact.setdefault(stamp, set()).add(raw.strip()[:10])
    if len(exact) == 1:
        stamp, declared_dates = next(iter(exact.items()))
        return stamp if dates <= declared_dates else ''
    return next(iter(dates)) if not exact and len(dates) == 1 else ''


def _publisher_excerpt(url, keywords, timeout):
    try:
        final, mime, data = safe_fetch(url, timeout_seconds=timeout, max_bytes=MAX_DOCUMENT_BYTES)
    except (OSError, ValueError):
        return None
    if not source_url(final) or '\\' in final:
        return {}
    original_path, final_path = urlsplit(url).path, urlsplit(final).path
    if original_path.strip('/') and not final_path.strip('/'):
        return {}
    media = mime.split(';', 1)[0].strip().lower()
    if media and media not in ('text/html', 'application/xhtml+xml'):
        return {}
    html, _ = _decode(data, mime)
    reader = _PublisherReader()
    try:
        reader.feed(html)
        reader.close()
        reader.finish()
    except (ValueError, RecursionError):
        return {}
    title = next((name for raw in (reader.meta.get('og:title'), ''.join(reader.h1), ''.join(reader.title))
                  if (name := _headline(raw, reader.meta.get('og:site_name', '')))), '')
    published = _consistent_publication(reader.date_literals(title))
    quotes = [(in_article, excerpt) for in_article, value in reader.paragraphs
              if len(excerpt := _description(value, title)) >= 30]
    # Select a genuine visible paragraph; metadata descriptions are not used.
    _, quote = max(quotes, key=lambda item: (item[0], sum(term in item[1].casefold() for term in keywords)),
                   default=(False, ''))
    return dict(url=final, title=title, quote=quote, published_at=published, kind='publisher_history')


def search_history(queries, lower, upper, *, event_text='', checkpoint=None):
    """Use one bounded archive query, retaining only dated publisher excerpts."""
    result = HistoryResult(diagnostics=dict(
        status='unconfigured', provider='gdelt_bigquery', date_from='', date_to='',
        archive_hit_count=0, lead_count=0, attempted_count=0, fetched_count=0, verified_count=0,
        missing_publication_count=0, outside_window_count=0, missing_excerpt_count=0,
        bytes_billed=0, window_limited=False, lead_limit_reached=False, verification_limited=False,
    ))
    report = result.diagnostics
    project = os.getenv(PROJECT_ENV, '').strip()
    if not project:
        return result
    try:
        lookback = _setting(DAYS_ENV, DEFAULT_LOOKBACK, 366)
        max_bytes = _setting(BYTES_ENV, DEFAULT_BYTES_BILLED, 1024 ** 4)
    except _HistoryError as exc:
        report.update(status='error', error_code=str(exc))
        return result
    floor, ceiling = _utc(lower), _utc(upper)
    if floor >= ceiling:
        report['status'] = 'out_of_range'
        return result
    end = ceiling.date() + timedelta(days=ceiling.time() != time.min)
    start = max(floor.date(), ARCHIVE_START, end - timedelta(days=min(lookback, end.toordinal() - 1)))
    if start >= end:
        report['status'] = 'out_of_range'
        return result
    report.update(date_from=start.isoformat(), date_to=(end - timedelta(days=1)).isoformat(),
                  window_limited=start > floor.date())
    keywords = _keywords(queries, event_text)
    if not keywords:
        report['status'] = 'no_keywords'
        return result
    if checkpoint is not None:
        checkpoint()
    try:
        rows, billed = _run_query(project, start, end, keywords, max_bytes)
    except _HistoryError as exc:
        report.update(status='error', error_code=str(exc))
        return result
    if checkpoint is not None:
        checkpoint()
    leads = {}
    for row in rows:
        url = source_url(row.get('url'))
        if url and '\\' not in url:
            leads.setdefault(canonicalize_url(url), (url, str(row['event_id'])))
    report.update(archive_hit_count=len(rows), lead_count=len(leads), bytes_billed=billed,
                  lead_limit_reached=len(rows) == MAX_LEADS)
    if not leads:
        report['status'] = 'no_results'
        return result
    deadline = monotonic() + PUBLISHER_BUDGET_SECONDS
    seen = set()
    for url, event_id in leads.values():
        timeout = min(FETCH_SECONDS, deadline - monotonic())
        if report['attempted_count'] == MAX_FETCHES or timeout <= 0:
            report['verification_limited'] = True
            break
        if checkpoint is not None:
            checkpoint()
        report['attempted_count'] += 1
        evidence = _publisher_excerpt(url, keywords, timeout)
        if evidence is None:
            continue
        report['fetched_count'] += 1
        stamp = interval(evidence.get('published_at'))
        if stamp is None:
            report['missing_publication_count'] += 1
            continue
        if not (floor <= stamp[0] < ceiling and stamp[1] <= ceiling):
            report['outside_window_count'] += 1
            continue
        if not evidence['title'] or not evidence['quote']:
            report['missing_excerpt_count'] += 1
            continue
        key = canonicalize_url(evidence['url'])
        if key in seen:
            continue
        seen.add(key)
        ref = 'gdelt-history:' + hashlib.sha256(key.encode()).hexdigest()
        evidence['gdelt_event_id'] = event_id
        result.candidates.append(dict(ref=ref, event_key='', occurred_at='',
            published_at=evidence['published_at'], similarity=0.,
            text=evidence['title'] + '\n' + evidence['quote'], sources=[evidence]))
    report.update(status='verified' if result.candidates else 'unverified',
                  verified_count=len(result.candidates))
    return result
