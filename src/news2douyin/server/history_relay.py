"""Receive official GDELT manifests and planned ZIPs from the local downloader."""
from contextlib import asynccontextmanager
import errno
from hashlib import md5
import hmac
import json
import os
from pathlib import Path
import re
from shutil import disk_usage
from tempfile import NamedTemporaryFile

import anyio
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from ..history_archive import catalog

_PREFIX = '/api/history-archive/relay'
_FREE_RESERVE = 2 * 1024 ** 3


def _access(request: Request, authorization: str = Header(default='')):
    worker = getattr(request.app.state, 'history_archive', None)
    if getattr(worker, 'archive_source', None) != 'relay':
        raise HTTPException(503, '历史归档尚未启用本地中继')
    token = os.getenv('NEWS2DOUYIN_HISTORY_RELAY_TOKEN', '').strip()
    if not token:
        raise HTTPException(503, '本地中继令牌尚未配置')
    scheme, _, supplied = authorization.partition(' ')
    if scheme.casefold() != 'bearer' or not hmac.compare_digest(supplied.encode(), token.encode()):
        raise HTTPException(401, '本地中继令牌无效', headers={'WWW-Authenticate': 'Bearer'})
    return worker


def _length(request, maximum):
    values = request.headers.getlist('content-length')
    if not values:
        raise HTTPException(411, '上传需要 Content-Length')
    if len(values) != 1 or not re.fullmatch(r'[0-9]{1,18}', values[0]) or 'transfer-encoding' in request.headers:
        raise HTTPException(400, '上传长度无效')
    length = int(values[0])
    if length <= 0:
        raise HTTPException(422, '上传内容为空')
    if length > maximum:
        raise HTTPException(413, '上传内容超过大小限制')
    return length


def _new_temp(root, length):
    incoming = root / '.relay-incoming'
    incoming.mkdir(exist_ok=True, parents=True)
    if disk_usage(incoming).free < length + _FREE_RESERVE:
        raise HTTPException(507, '历史归档剩余磁盘空间不足')
    return NamedTemporaryFile(dir=incoming, suffix='.part', delete=False)


def _sync_close(file):
    file.flush()
    os.fsync(file.fileno())
    file.close()


@asynccontextmanager
async def _receive(request, root, length, *, checksum=False):
    digest = md5(usedforsecurity=False) if checksum else None
    file = None
    try:
        file = await run_in_threadpool(_new_temp, root, length)
        count = 0

        def append(block):
            if digest is not None:
                digest.update(block)
            file.write(block)

        async for block in request.stream():
            count += len(block)
            if count > length:
                raise HTTPException(422, '上传字节数与声明不符')
            if block:
                await run_in_threadpool(append, block)
        if count != length:
            raise HTTPException(422, '上传不完整')
        await run_in_threadpool(_sync_close, file)
        yield Path(file.name), digest.hexdigest() if digest is not None else None
    except ClientDisconnect:
        raise HTTPException(400, '上传已中断') from None
    except OSError as exc:
        if exc.errno in (errno.ENOSPC, getattr(errno, 'EDQUOT', None)):
            raise HTTPException(507, '历史归档剩余磁盘空间不足') from None
        raise
    finally:
        if file is not None:
            with anyio.CancelScope(shield=True):
                try:
                    if not file.closed:
                        await run_in_threadpool(file.close)
                finally:
                    await run_in_threadpool(Path(file.name).unlink, missing_ok=True)


def _valid_catalog(path, relative):
    if not catalog._looks_like_catalog(path, relative):
        return False
    # A connection can close in the middle of a manifest line even upstream
    # of the relay. Accept a complete final record with or without its newline.
    with path.open('rb') as source:
        source.seek(max(0, path.stat().st_size - 4096))
        tail = source.read().rstrip().splitlines()
    if not tail:
        return False
    fields = tail[-1].split()
    if relative == catalog._MASTER:
        return (len(fields) == 3 and fields[0].isdigit()
                and bool(re.fullmatch(rb'[0-9a-fA-F]{32}', fields[1]))
                and bool(re.fullmatch(rb'https?://data\.gdeltproject\.org/gdeltv2/[^/\s]+\.zip', fields[2])))
    if len(fields) != 2 or not fields[1].endswith(b'.zip'):
        return False
    return fields[0].isdigit() if relative.endswith('/filesizes') else bool(re.fullmatch(rb'[0-9a-fA-F]{32}', fields[0]))


def _publish_catalog(worker, relative, path):
    if not _valid_catalog(path, relative):
        raise HTTPException(422, '上传内容与官方目录格式不符')
    directory = worker.root / 'relay-catalog'
    directory.mkdir(exist_ok=True, parents=True)
    os.replace(path, directory / relative.replace('/', '-'))
    catalog._sync_directory(directory)
    worker.wake_event.set()


def _call(fn, *args):
    try:
        return fn(*args)
    except KeyError:
        raise HTTPException(404, '未找到该归档任务') from None
    except ValueError:
        raise HTTPException(422, '归档文件或校验值不匹配') from None
    except RuntimeError:
        raise HTTPException(409, '当前归档状态无法接收，请稍后重试') from None


def _reply(data):
    return JSONResponse(data, headers={'Cache-Control': 'no-store'})


def register_relay(app):
    @app.get('/admin/history-archive/relay-client.py', include_in_schema=False)
    def client_script():
        script = Path(catalog.__file__).with_name('relay_client.py')
        return FileResponse(script, filename='gdelt_history_relay.py', media_type='text/plain')

    router = APIRouter(prefix=_PREFIX)

    @router.get('/work')
    def work(limit: int = Query(96, ge=1, le=96), worker=Depends(_access)):
        return _reply(worker.relay_work(limit=limit))

    @router.put('/catalog/{relative:path}')
    async def catalog_upload(relative: str, request: Request, worker=Depends(_access)):
        if relative not in catalog.RELAY_CATALOGS:
            raise HTTPException(404, '未知归档目录')
        maximum = catalog._MASTER_LIMIT if relative == catalog._MASTER else catalog._LEGACY_LIMIT
        length = _length(request, maximum)
        async with _receive(request, worker.root, length) as (temp_path, _):
            await run_in_threadpool(_publish_catalog, worker, relative, temp_path)
        return _reply({'state': 'accepted', 'catalog': relative})

    @router.put('/packages/{name}')
    async def package_upload(name: str, request: Request, worker=Depends(_access)):
        part = await run_in_threadpool(_call, worker.relay_part, name)
        length = _length(request, part['size'])
        if length != part['size']:
            raise HTTPException(422, '上传长度与官方目录不符')
        if part['state'] == 'ready':
            return _reply({'name': name, 'state': 'ready'})
        async with _receive(request, worker.root, length, checksum=True) as (temp_path, digest):
            if part['md5'] != digest:
                raise HTTPException(422, '归档校验值与官方目录不符')
            result = await run_in_threadpool(_call, worker.relay_receive, name, temp_path, length, digest)
        return _reply(result)

    @router.post('/packages/{name}/failure')
    async def failure(name: str, request: Request, worker=Depends(_access)):
        body = bytearray()
        async for block in request.stream():
            if len(body) + len(block) > 256:
                raise HTTPException(413, '状态记录超出大小限制')
            body.extend(block)
        try:
            value = json.loads(body)
        except (ValueError, RecursionError):
            value = None
        if (not isinstance(value, dict) or value.keys() != {'status'}
                or type(value['status']) is not int or value['status'] not in (404, 410)):
            raise HTTPException(422, '仅可记录官方文件返回的 404 或 410')
        return _reply(await run_in_threadpool(_call, worker.relay_fail, name, value['status']))

    app.include_router(router)
