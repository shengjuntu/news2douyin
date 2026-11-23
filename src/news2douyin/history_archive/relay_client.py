"""Download GDELT archives on this computer and deliver them to news2douyin.

Run with Python 3.10 or later. All imports are from the standard library.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import md5, sha256
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


OFFICIAL = 'https://data.gdeltproject.org/'
API = '/api/history-archive/relay'
TOKEN_ENV = 'NEWS2DOUYIN_HISTORY_RELAY_TOKEN'
CATALOGS = {'gdeltv2/masterfilelist.txt': 256 << 20,
            **{f'{kind}/{part}': 4 << 20 for kind in ('events', 'gkg') for part in ('filesizes', 'md5sums')}}
BLOCK = 256 << 10
WAIT_LABELS = {'catalog': '等待官方目录', 'planning': '服务器正在规划',
               'uploads': '等待上传', 'conversion': '服务器正在转换', 'idle': '等待补档任务',
               'paused': '服务器已暂停'}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TransferError(Exception):
    def __init__(self, message, *, status=0, delay=0):
        super().__init__(message)
        self.status, self.delay = status, delay


TRANSFER_ERRORS = (TransferError, OSError, HTTPException)


def retry_after(value):
    if value and value.isascii() and value.isdigit() and len(value) < 10:
        return int(value)
    if value:
        try:
            moment = parsedate_to_datetime(value)
            return max(0, (moment - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            pass
    return 0


@dataclass(frozen=True)
class Ticket:
    relative: str
    size: int
    digest: str

    @property
    def is_catalog(self):
        return self.relative in CATALOGS

    @property
    def name(self):
        return self.relative.rsplit('/', 1)[-1]


def package_ticket(row):
    if not isinstance(row, dict):
        raise ValueError('任务文件信息无效')
    name, size, digest = (row.get(key) for key in ('name', 'size', 'md5'))
    if not isinstance(name, str) or type(size) is not int or size < 1:
        raise ValueError('任务文件名或大小无效')
    if not isinstance(digest, str) or not re.fullmatch('[a-fA-F0-9]{32}', digest):
        raise ValueError('任务 MD5 无效')
    if re.fullmatch(r'[0-9]{14}\.(?:export\.CSV|mentions\.CSV|gkg\.csv)\.zip', name):
        folder = 'gdeltv2'
    elif re.fullmatch(r'[0-9]{8}\.gkg\.csv\.zip', name):
        folder = 'gkg'
    elif re.fullmatch(r'(?:[0-9]{4}|[0-9]{6}|[0-9]{8}\.export\.CSV)\.zip', name):
        folder = 'events'
    else:
        raise ValueError('任务文件不是支持的英文 GDELT 归档')
    stamp = name.partition('.')[0]
    try:
        if folder == 'gdeltv2':
            clock = datetime.strptime(stamp, '%Y%m%d%H%M%S')
            valid = stamp >= '20150218230000' and clock.minute % 15 == 0 and clock.second == 0
        elif len(stamp) == 4:
            valid = '1979' <= stamp <= '2005'
        elif len(stamp) == 6:
            datetime.strptime(stamp, '%Y%m')
            valid = '200601' <= stamp <= '201303'
        else:
            datetime.strptime(stamp, '%Y%m%d')
            valid = '20130401' <= stamp <= '20150218'
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('任务文件日期不在支持的官方归档范围内')
    relative = folder + '/' + name
    if row.get('url') != OFFICIAL + relative:
        raise ValueError('任务地址与官方归档路径不符')
    return Ticket(relative, size, digest.lower())


def md5_file(path):
    digest = md5(usedforsecurity=False)
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(BLOCK), b''):
            digest.update(chunk)
    return digest.hexdigest()


class Relay:
    def __init__(self, server, token, cache_dir, *, limit=32):
        server = server.strip().rstrip('/')
        parsed = urlsplit(server)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment or parsed.query):
            raise ValueError('--server 应为 news2douyin 的 HTTP(S) 服务地址')
        if not token or not token.isascii() or any(ord(c) <= 32 or ord(c) >= 127 for c in token):
            raise ValueError('请在环境变量 ' + TOKEN_ENV + ' 中设置中继令牌')
        if not 1 <= limit <= 96:
            raise ValueError('--limit 应为 1 到 96')
        self.server, self.token, self.limit = server + API, token, limit
        self.cache = Path(cache_dir).expanduser() / sha256(server.encode()).hexdigest()[:16]
        self.cache.mkdir(parents=True, exist_ok=True)
        self.upstream = build_opener(NoRedirect())
        self.downstream = build_opener(NoRedirect())
        self.waiting = ''

    def open_official(self, relative):
        return self.upstream.open(Request(OFFICIAL + relative, headers={'Accept-Encoding': 'identity'}), timeout=120)

    def api(self, path, *, method='GET', body=None, size=0, content_type=None):
        headers = {'Authorization': 'Bearer ' + self.token, 'Accept': 'application/json'}
        if content_type:
            headers.update({'Content-Type': content_type, 'Content-Length': str(size)})
        req = Request(self.server + path, method=method, headers=headers, data=body)
        try:
            with self.downstream.open(req, timeout=180) as response:
                data = response.read((1 << 20) + 1)
                if len(data) > (1 << 20):
                    raise TransferError('news2douyin 响应过大')
                result = json.loads(data)
                if not isinstance(result, dict):
                    raise ValueError()
                return result
        except HTTPError as exc:
            exc.close()
            raise TransferError(f'news2douyin HTTP {exc.code}', status=exc.code,
                                delay=retry_after(exc.headers.get('Retry-After'))) from None
        except (ValueError, UnicodeError):
            raise TransferError('news2douyin 响应格式无效') from None

    def _download(self, ticket):
        part = self.cache / 'download.part'
        limit = CATALOGS.get(ticket.relative, ticket.size)
        received, digest = 0, md5(usedforsecurity=False)
        next_update = time.monotonic() + 10
        print(f'正在下载 {ticket.relative}', flush=True)
        try:
            with self.open_official(ticket.relative) as response, part.open('wb') as output:
                if response.status != 200 or response.headers.get('Content-Range') is not None:
                    raise TransferError(f'{ticket.name} 官网未返回完整文件')
                header = response.headers.get('Content-Length')
                if header is not None and (not header.isdigit() or not 0 < int(header) <= limit):
                    raise TransferError(f'{ticket.name} 官方文件大小异常')
                if header is None and not ticket.size and response.headers.get('Transfer-Encoding','').lower() != 'chunked':
                    raise TransferError(f'{ticket.name} 官方目录缺少可验证的传输长度')
                for chunk in iter(lambda: response.read(BLOCK), b''):
                    received += len(chunk)
                    if received > limit:
                        raise TransferError(f'{ticket.name} 超过预期大小')
                    output.write(chunk)
                    digest.update(chunk)
                    if time.monotonic() >= next_update:
                        print(f'{ticket.name} 已下载 {received / 1024 ** 2:.1f} MiB', flush=True)
                        next_update = time.monotonic() + 10
                if (not received or (header is not None and received != int(header))
                        or (ticket.size and (received != ticket.size or digest.hexdigest() != ticket.digest))):
                    raise TransferError(f'{ticket.name} 下载长度或 MD5 校验失败')
                output.flush()
                os.fsync(output.fileno())
            # At most one in-flight delivery. The journal survives a lost PUT
            # response even when the server no longer lists the original ticket.
            receipt = Ticket(ticket.relative, received, digest.hexdigest())
            os.replace(part, self.cache / 'payload')
            journal = self.cache / 'pending.tmp'
            with journal.open('w', encoding='utf-8') as out:
                json.dump(asdict(receipt), out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(journal, self.cache / 'pending.json')
            return receipt
        finally:
            part.unlink(missing_ok=True)

    def _send(self, ticket):
        payload = self.cache / 'payload'
        try:
            with payload.open('rb') as stream:
                path = '/catalog/' + ticket.relative if ticket.is_catalog else '/packages/' + ticket.name
                ack = self.api(path, method='PUT', body=stream, size=ticket.size, content_type='application/octet-stream')
        except TransferError as exc:
            # The receiver explicitly rejected these bytes. Refetch the current
            # official plan next time instead of replaying an invalid catalogue.
            if exc.status in (404, 422):
                self._discard_pending()
            raise
        if ticket.is_catalog:
            accepted = ack.get('catalog') == ticket.relative and ack.get('state') == 'accepted'
        else:
            accepted = ack.get('name') == ticket.name and ack.get('state') in ('ready', 'staged')
        if not accepted:
            raise TransferError(f'{ticket.name} 的上传回执不匹配，保留缓存')
        self._discard_pending()
        print(f'已上传 {ticket.relative} · {ticket.size:,} 字节', flush=True)

    def _discard_pending(self):
        (self.cache / 'pending.json').unlink(missing_ok=True)
        (self.cache / 'payload').unlink(missing_ok=True)

    def _pending(self):
        journal = self.cache / 'pending.json'
        if not journal.exists():
            return None
        with journal.open(encoding='utf-8') as f:
            row = json.load(f)
        relative = row['relative']
        if relative in CATALOGS:
            ticket = Ticket(relative, row['size'], row['digest'])
            if not 0 < ticket.size <= CATALOGS[relative] or not re.fullmatch('[0-9a-f]{32}', ticket.digest):
                raise ValueError('本地待传目录记录无效')
        else:
            ticket = package_ticket(dict(name=relative.rsplit('/', 1)[-1], size=row['size'],
                                         md5=row['digest'], url=OFFICIAL + relative))
        payload = self.cache / 'payload'
        catalog_stale = (ticket.is_catalog and time.time() - journal.stat().st_mtime >= 3600)
        if catalog_stale or not payload.is_file() or payload.stat().st_size != ticket.size or md5_file(payload) != ticket.digest:
            # Server will issue the ticket again if it still needs the bytes.
            self._discard_pending()
            return None
        return ticket

    def run_once(self):
        work = self.api(f'/work?limit={self.limit}')
        self.waiting = 'paused' if work.get('paused') else work.get('waiting', '')
        if work.get('paused'):
            return 0
        if not isinstance(work.get('catalogs'), list) or not isinstance(work.get('packages'), list):
            raise TransferError('中继任务清单格式无效')
        tickets = []
        for catalog in work['catalogs']:
            if catalog not in CATALOGS:
                raise ValueError('目录地址不属于官方 GDELT 清单')
            tickets.append(Ticket(catalog, 0, ''))
        tickets.extend(package_ticket(row) for row in work['packages'])
        sent = 0
        pending = self._pending()
        if pending:
            self._send(pending)
            sent += 1
        failures = []
        for ticket in tickets:
            if pending and pending.relative == ticket.relative:
                continue
            try:
                downloaded = self._download(ticket)
            except HTTPError as exc:
                exc.close()
                if exc.code in (404, 410) and not ticket.is_catalog:
                    body = json.dumps({'status': exc.code}).encode()
                    ack = self.api('/packages/' + ticket.name + '/failure', method='POST',
                                   content_type='application/json', body=body, size=len(body))
                    if ack.get('name') != ticket.name or ack.get('state') not in ('failed', 'ready', 'staged'):
                        raise TransferError('文件缺失回执无效')
                    print(f'{ticket.name} 官网返回 {exc.code}，已登记', file=sys.stderr, flush=True)
                    continue
                delay = retry_after(exc.headers.get('Retry-After'))
                error = TransferError(f'{ticket.name} GDELT HTTP {exc.code}', delay=max(30, delay) if exc.code == 429 else delay)
                if exc.code == 429 or error.delay:
                    raise error from None
                failures.append(str(error))
                continue
            except TRANSFER_ERRORS as exc:
                failures.append(f'{ticket.name} 下载失败 · {exc}')
                continue
            try:
                self._send(downloaded)
            except TransferError as exc:
                if exc.status not in (404, 422):
                    raise
                failures.append(f'{ticket.name} 上传被拒绝 · {exc}')
                continue
            sent += 1
        if failures:
            print('\n'.join(failures), file=sys.stderr, flush=True)
            raise TransferError(f'本批 {len(failures)} 个文件需要重试')
        return sent


@contextmanager
def single_instance(folder):
    lock = (folder / 'relay.lock').open('a+b')
    try:
        try:
            if os.name == 'nt':
                import msvcrt
                if not lock.seek(0, os.SEEK_END):
                    lock.write(b'\0')
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError('该缓存目录已有本地中继在运行') from None
        yield
    finally:
        lock.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='在本机下载 GDELT 历史归档并上传到 news2douyin')
    parser.add_argument('--server', required=True, help='news2douyin 服务地址')
    parser.add_argument('--cache-dir', default='gdelt-relay-cache', help='本地缓存目录')
    parser.add_argument('--limit', type=int, default=32, help='每批最多 ZIP 数，1–96，默认 32')
    parser.add_argument('--poll-seconds', type=int, default=15, help='空闲检查间隔秒数，默认 15')
    parser.add_argument('--once', action='store_true', help='领取并执行当前一批，随后退出')
    args = parser.parse_args(argv)
    if args.poll_seconds < 1:
        parser.error('--poll-seconds 必须为正整数')
    try:
        relay = Relay(args.server, os.environ.get(TOKEN_ENV, ''), args.cache_dir, limit=args.limit)
        with single_instance(relay.cache):
            # Clean only interrupted writes in the private, instance-locked spool.
            (relay.cache / 'download.part').unlink(missing_ok=True)
            (relay.cache / 'pending.tmp').unlink(missing_ok=True)
            if not (relay.cache / 'pending.json').exists():
                (relay.cache / 'payload').unlink(missing_ok=True)
            delay, last_state = 0, None
            while True:
                try:
                    sent = relay.run_once()
                    delay = 0
                    if not sent and relay.waiting != last_state:
                        print('服务器队列状态：' + WAIT_LABELS.get(relay.waiting, relay.waiting or '等待补档任务'), flush=True)
                    last_state = relay.waiting
                    if args.once:
                        return 0
                    if not sent:
                        time.sleep(args.poll_seconds)
                except TRANSFER_ERRORS as exc:
                    print(str(exc), file=sys.stderr, flush=True)
                    if args.once or getattr(exc, 'status', None) in (401, 403):
                        return 1
                    delay = min(120, max(2, delay * 2))
                    time.sleep(max(delay, getattr(exc, 'delay', 0)))
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
