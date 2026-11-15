"""Small, durable per-feed evidence about acquisition requests.

No response bodies, credentials or article payloads are copied here. Ordinary
status reads are SQL-only; the explicit probe command performs bounded streaming
requests and never changes intake cursors, archives or retry policy.
"""
from __future__ import annotations

# ``python -m`` must use the canonical module's model declaration; declaring
# the same SQLModel table under both __main__ and its package name duplicates it.
if __name__ == '__main__':
    from .fetch_diagnostics import main as _main
    raise SystemExit(_main())

import argparse
import json
from urllib.parse import urlsplit
from zipfile import BadZipFile

import requests
from sqlmodel import SQLModel, Field, select

from ..storage.models import utc_now_iso
from .feeds import FEEDS


class IntakeFetchDiagnostic(SQLModel, table=True):
    feed: str = Field(primary_key=True)
    detail_json: str = '{}'
    updated_at: str = Field(default_factory=utc_now_iso)


def _public_url(value):
    """Diagnostics do not echo an exception's URL or a response redirect."""
    if not isinstance(value, str) or len(value) > 2048:
        return ''
    try:
        p = urlsplit(value)
        if p.scheme not in ('http', 'https') or p.username or p.password or p.query or p.fragment:
            return ''
        if p.hostname != 'data.gdeltproject.org' or p.port not in (None, 80, 443):
            return ''
        if not p.path.startswith('/gdeltv2/') or any(ord(c) < 33 for c in value):
            return ''
        return value
    except ValueError:
        return ''


def error_details(exc, phase):
    """Classify evidence without deciding that a 404 is a permanent gap."""
    status = getattr(exc, 'status', None)
    if not isinstance(status, int):
        status = getattr(exc, 'intake_http_status', None)
    if status == 404:
        kind = 'manifest_not_found' if phase == 'manifest' else 'batch_not_found_unverified'
        message = '官方清单请求返回404' if phase == 'manifest' else '目标批次返回404；尚未确认是缺档、发布时间差或服务侧问题，游标未跳过'
    elif status == 429:
        kind, message = 'rate_limited', '上游限流，按重试时间等待'
    elif isinstance(status, int) and 300 <= status < 400:
        kind, message = 'http_redirect', '上游返回重定向；未自动跟随到其他地址'
    elif isinstance(status, int) and status != 200:
        kind, message = 'http_error', 'HTTP请求未成功，状态码见请求详情'
    elif isinstance(exc, requests.exceptions.SSLError):
        kind, message = 'tls_error', 'TLS连接失败，未降低为不安全的传输方式'
    elif isinstance(exc, requests.exceptions.Timeout):
        kind, message = 'timeout', '连接或读取超时，沿用当前接收超时配置'
    elif isinstance(exc, requests.exceptions.ConnectionError):
        kind, message = 'connection_error', '连接未建立，尚未取得有效HTTP响应'
    elif isinstance(exc, InterruptedError):
        kind, message = 'interrupted', '接收已停止；未推进游标'
    elif isinstance(exc, BadZipFile):
        kind, message = 'archive_invalid', '响应不能作为有效ZIP归档处理'
    elif isinstance(exc, ValueError):
        code = str(exc)
        if code.startswith('update_manifest_missing_'):
            kind, message = 'manifest_missing_feed', '已读取清单，但未找到该流的可信文件条目'
        elif code == 'update_manifest_too_large':
            kind, message = 'manifest_size_limit', '清单超过允许大小'
        elif code in ('archive_md5_mismatch', 'archive_sha256_mismatch') or 'checksum' in code:
            kind, message = 'archive_checksum', '归档校验失败，未发布该批次'
        elif code == 'archive_size_mismatch':
            kind, message = 'archive_size_mismatch', '归档大小与清单不符，未发布该批次'
        elif code == 'archive_download_size_limit':
            kind, message = 'archive_size_limit', '响应超过允许的归档大小'
        else:
            kind, message = ('manifest_invalid', '清单未通过解析校验') if phase == 'manifest' else ('archive_invalid', '归档或来源字段未通过校验')
    elif isinstance(exc, OSError):
        kind, message = 'archive_io_error', '本地归档读写失败，未推进游标'
    else:
        kind, message = 'internal_error', '处理失败；原始输入和游标保留以供重试'
    return dict(http_status=status if isinstance(status, int) else None, error_kind=kind, message=message)


def snapshot(feed, **values):
    if feed not in FEEDS:
        raise ValueError('未知接收流')
    data = dict(feed=feed, attempt_id='', status='running', phase='manifest', request_url='',
        target_stamp='', latest_stamp='', attempted_at=utc_now_iso(), completed_at='',
        http_status=None, error_kind='', message='', expected_md5='', expected_bytes=0,
        manifest_url='', manifest_listed_url='', manifest_http_status=None,
        archive_reused=False, transport_policy='https_only')
    data.update(values)
    return public(data)


def public(data):
    result = dict(data)
    for name in ('request_url', 'manifest_url', 'manifest_listed_url'):
        result[name] = _public_url(result.get(name, ''))
    # Aliases allow older status consumers to adopt diagnostics incrementally.
    result['url'] = result.get('request_url', '')
    result['latest_manifest_stamp'] = result.get('latest_stamp', '')
    result['requested_at'] = result.get('attempted_at', '')
    return result


def save(session, feed, details):
    """Caller already holds the feed's lease lock and owns the transaction."""
    from .models import FeedState
    state = session.get(FeedState, feed)
    row = session.get(IntakeFetchDiagnostic, feed)
    try:
        saved_token = json.loads(row.detail_json).get('attempt_id', '') if row else ''
    except (ValueError, TypeError, AttributeError):
        saved_token = ''
    token = details.get('attempt_id', '')
    # The caller holds the feed lock through check and publication; this token
    # check additionally rejects a stale receiver after ownership has changed.
    if not state or not token or (state.lease != token and (state.lease or saved_token != token)):
        return False
    row = row or IntakeFetchDiagnostic(feed=feed)
    row.detail_json = json.dumps(public(details), ensure_ascii=False, separators=(',', ':'))
    row.updated_at = utc_now_iso()
    session.add(row)
    return True


def list_saved(session, owners=None):
    result = {}
    for row in session.exec(select(IntakeFetchDiagnostic).order_by(IntakeFetchDiagnostic.feed).limit(len(FEEDS))):
        try:
            value = json.loads(row.detail_json)
            if isinstance(value, dict):
                if owners is not None and value.get('status')=='running' and owners.get(row.feed)!=value.get('attempt_id'):
                    value.update(status='interrupted',message='上次接收未完成，等待恢复租约后重试；未推进游标')
                result[row.feed] = public(value)
        except (ValueError, TypeError):
            result[row.feed] = dict(feed=row.feed, status='failed', error_kind='diagnostic_invalid',
                message='上次请求诊断无法解析，等待下一次接收刷新')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--feed', choices=tuple(FEEDS), required=True)
    parser.add_argument('--db-url', default=None, help='Uses the normal application database by default; do not put credentials in shell history')
    args = parser.parse_args(argv)
    from ..config import load_environment
    from ..storage.db import default_db_url
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from pathlib import Path
    from .service import probe
    load_environment()
    engine = None
    try:
        url=make_url(args.db_url or default_db_url())
        backend=url.get_backend_name()
        if backend in ('postgresql','postgres'):
            url=url.set(drivername='postgresql+psycopg')
            options=dict(connect_timeout=10,options='-c default_transaction_read_only=on -c statement_timeout=15000')
        elif backend=='sqlite':
            path=Path(url.database or '').resolve()
            if not path.is_file():raise ValueError('Existing SQLite database required')
            url=url.set(database='file:'+path.as_posix()).update_query_dict(dict(mode='ro',uri='true'))
            options=dict(check_same_thread=False)
        else:
            raise ValueError('Unsupported database')
        # Do not run application initialization or set SQLite WAL from a
        # diagnostic command. Both backends enforce a read-only connection.
        engine=create_engine(url,hide_parameters=True,connect_args=options)
        result = probe(engine, args.feed)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['manifest']['status'] == 'succeeded' and result['target']['status'] == 'succeeded' else 1
    except Exception as exc:
        print(json.dumps(dict(status='failed',error_kind='diagnostic_unavailable',
            error_type=type(exc).__name__,message='无法读取已初始化的应用数据库；未更改游标或配置'),ensure_ascii=False))
        return 2
    finally:
        if engine is not None:engine.dispose()


