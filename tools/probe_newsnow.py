"""Read-only NewsNow JSON protocol probe; run on the deployment host.

The probe calls only GET /api/s?id= through the regular provider. It does not
write Article/Event rows, start application workers, or contact article URLs.
Returned timestamps are NewsNow's reported snapshot times, not a promise of a
new upstream fetch. Exit codes: 0 complete, 2 partial, 3 all sources failed,
4 complete but empty, 1 invalid configuration or unexpected execution error.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

DEFAULT_PROBE_SOURCES = ('zhihu', 'cls', 'douyin')
SOURCE_FIELDS = ('source_id', 'name', 'kind', 'status', 'http_status', 'upstream_status', 'response_status',
    'raw_items', 'valid_items', 'invalid_items', 'selected_items', 'elapsed_ms',
    'observed_at', 'snapshot_reported_at', 'snapshot_time_basis', 'payload_sha256')
SUMMARY_FIELDS = ('requested_sources', 'succeeded_sources', 'failed_sources',
    'empty_sources', 'raw_items', 'valid_items', 'selected_items', 'truncated', 'status')


def _provider():
    from news2douyin.collect.providers import newsnow
    return newsnow


def safe_base(value):
    """Display only origin/path; never userinfo, query values, or a fragment."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ''
        if not host or parsed.scheme not in ('http', 'https'):
            return ''
        if ':' in host:
            host = '[' + host + ']'
        authority = host + (':' + str(parsed.port) if parsed.port else '')
        return urlunsplit((parsed.scheme, authority, parsed.path.rstrip('/'), '', ''))
    except (ValueError, TypeError):
        return ''


def summarize(batch, base_url):
    summary = {key: batch.get('summary', {}).get(key) for key in SUMMARY_FIELDS
               if key in batch.get('summary', {})}
    snapshots = {row.get('source_id'): row for row in batch.get('snapshots', [])
                 if isinstance(row, dict)}
    sources = []
    for row in batch.get('sources', []):
        if not isinstance(row, dict):
            continue
        item = {key: row[key] for key in SOURCE_FIELDS if key in row}
        snapshot = snapshots.get(row.get('source_id'), {})
        for key in ('observed_at', 'response_status', 'snapshot_reported_at', 'snapshot_time_basis'):
            if key not in item and key in snapshot:
                item[key] = snapshot[key]
        # Categories help diagnose protocol problems without copying a response
        # body, transport exception, cookies, credentials, or arbitrary URLs.
        error = row.get('error_kind') or row.get('error_code') or row.get('error', '')
        if isinstance(error, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', error):
            item['error_kind'] = error
        sources.append(item)
    return dict(service='newsnow', base_url=safe_base(base_url),
        checked_at=datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
        endpoint='/api/s?id=<source>', summary=summary, sources=sources,
        scope='只读协议与可用性探测；未写入新闻库，未验证文章全文或实际传播热度。',
        freshness_note='reported snapshot time来自NewsNow响应；success或新的响应时间不保证上游重新抓取。')


def probe(config, *, provider=None):
    provider = provider or _provider()
    all_failed = False
    try:
        batch = provider.fetch_batch(config)
    except provider.NewsNowUnavailable as exc:
        batch = exc.batch
        all_failed = True
    report = summarize(batch, config['base_url'])
    summary = report['summary']
    if all_failed or (summary.get('failed_sources') and not summary.get('succeeded_sources')):
        code = 3
    elif summary.get('failed_sources'):
        code = 2
    elif not summary.get('raw_items'):
        code = 4
    else:
        code = 0
    report['exit_code'] = code
    return report, code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True, help='NewsNow服务地址，例如 http://192.168.0.234:4444')
    parser.add_argument('--sources', default=','.join(DEFAULT_PROBE_SOURCES),
        help='逗号分隔的源ID；默认zhihu,cls,douyin；all显式探测配置的默认全源')
    parser.add_argument('--output', type=Path, help='可选：保存安全JSON诊断报告')
    args = parser.parse_args(argv)
    provider = _provider()
    sources = list(provider.DEFAULT_SOURCES) if args.sources.strip() == 'all' else [
        word.strip() for word in args.sources.split(',') if word.strip()]
    config = dict(base_url=args.base_url, newsnow_sources=sources, max_items=1200)
    try:
        report, code = probe(config, provider=provider)
    except Exception as exc:
        # A pasted URL might contain credentials. Do not echo it or str(exc).
        report = dict(service='newsnow', base_url=safe_base(args.base_url), exit_code=1,
            error_kind=type(exc).__name__,
            note='配置或运行错误，请检查服务地址、源ID和已安装版本；未写入业务数据库。')
        code = 1
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding='utf-8')
    print(serialized, end='')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
