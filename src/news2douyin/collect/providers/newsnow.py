"""Read saved NewsNow JSON lists; preserve observation and publication separately.

NewsNow is a discovery feed. List position is not an official rank, a hover
snippet is not an article body, and ``updatedTime`` is only the timestamp the
upstream API reports (some cached responses can report the request time).
"""
from __future__ import annotations

import base64
from collections import deque
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import os
import re
from time import monotonic
from urllib.parse import urlsplit, urlunsplit

import requests

CN_TZ=timezone(timedelta(hours=8))
DEFAULT_BASE_URL='http://192.168.0.234:4444'
MAX_BATCH_RESPONSE_BYTES=32*1024*1024
_DATE_RE=re.compile(r'^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?)?$')
_SOURCE_RE=re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}')

_CATALOG_ROWS=(
    ('zhihu','知乎','hottest','china'),('weibo','微博','hottest','china'),
    ('baidu','百度热搜','hottest','china'),('toutiao','今日头条','hottest','china'),
    ('douyin','抖音','hottest','china'),('tieba','百度贴吧','hottest','china'),
    ('bilibili','哔哩哔哩','hottest','china'),('kuaishou','快手','hottest','china'),
    ('douban','豆瓣','hottest','china'),('thepaper','澎湃新闻','hottest','china'),
    ('tencent','腾讯新闻','hottest','china'),('ifeng','凤凰网','hottest','china'),
    ('nowcoder','牛客','hottest','china'),('chongbuluo','虫部落','realtime','china'),
    ('freebuf','Freebuf','hottest','china'),('qqvideo','腾讯视频','hottest','china'),
    ('iqiyi','爱奇艺','hottest','china'),('aihot','AIHOT','realtime','tech'),
    ('ithome','IT之家','realtime','tech'),('solidot','Solidot','realtime','tech'),
    ('sspai','少数派','hottest','tech'),('juejin','稀土掘金','hottest','tech'),
    ('coolapk','酷安','hottest','tech'),('github','GitHub','hottest','tech'),
    ('producthunt','Product Hunt','realtime','tech'),
    ('wallstreetcn','华尔街见闻','realtime','finance'),('cls','财联社','realtime','finance'),
    ('jin10','金十数据','realtime','finance'),('gelonghui','格隆汇','realtime','finance'),
    ('xueqiu','雪球股票榜','non_news','finance'),('fastbull','法布财经','realtime','finance'),
    ('cankaoxiaoxi','参考消息','realtime','world'),('sputniknewscn','卫星通讯社','realtime','world'),
    ('dongqiudi','懂球帝','realtime','sports'),('hupu','虎扑','hottest','sports'),
)
SOURCE_CATALOG={key:dict(name=name,kind=kind,category=category,is_news=kind!='non_news')
                for key,name,kind,category in _CATALOG_ROWS}
SOURCE_NAMES={key:value['name'] for key,value in SOURCE_CATALOG.items()}
DEFAULT_SOURCES=list(SOURCE_CATALOG)
# Some NewsNow versions redirect parent names before echoing the source id.
# Only code-owned, known aliases are accepted; an arbitrary different id fails.
SOURCE_ALIASES={'tencent':'tencent-hot','qqvideo':'qqvideo-tv-hotsearch','iqiyi':'iqiyi-hot-ranklist'}


class NewsNowUnavailable(RuntimeError):
    """All sources failed; the complete bounded diagnostic batch is retained."""
    def __init__(self,batch):
        self.batch=batch
        super().__init__('NewsNow 所选来源均未成功读取，请检查服务地址、来源和逐源诊断。')


class _SourceFailure(Exception):
    def __init__(self,code,message):
        self.code=code
        super().__init__(message)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')


def _now():
    return _iso(datetime.now(timezone.utc))


def _number(config,name,default,low,high,*,integer=False):
    value=config.get(name,default)
    if isinstance(value,bool):raise ValueError(f'{name} 须为有效数值')
    try:result=float(value)
    except (TypeError,ValueError,OverflowError):raise ValueError(f'{name} 须为有效数值') from None
    if not math.isfinite(result) or not low<=result<=high or (integer and result!=int(result)):
        raise ValueError(f'{name} 须在 {low}–{high} 范围内'+('，且为整数' if integer else ''))
    return int(result) if integer else result


def _url(value,*,base=False):
    if not isinstance(value,str) or not value or len(value)>8192 or any(ord(c)<33 for c in value) or '\\' in value:
        raise ValueError('NewsNow 地址须为完整 HTTP(S) 地址，不含账号、密码或控制字符')
    try:
        parsed=urlsplit(value)
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError()
        parsed.port
        if base and (parsed.query or parsed.fragment):raise ValueError()
    except (ValueError,TypeError):raise ValueError('NewsNow 地址须为 HTTP(S)，且服务地址不含账号、密码、查询参数或片段') from None
    return urlunsplit(parsed).rstrip('/') if base else value


def validate(config):
    """Return normalized options without making network requests."""
    base=config.get('base_url') or os.getenv('NEWS2DOUYIN_NEWSNOW_BASE_URL','') or DEFAULT_BASE_URL
    base=_url(base,base=True)
    sources=config.get('newsnow_sources',DEFAULT_SOURCES)
    if not isinstance(sources,(list,tuple)) or not 1<=len(sources)<=100:
        raise ValueError('NewsNow 须选择1–100个来源')
    if any(not isinstance(source,str) or not _SOURCE_RE.fullmatch(source) for source in sources):
        raise ValueError('NewsNow 来源编号仅支持字母、数字、下划线、点与连字符，最长80字符')
    sources=list(dict.fromkeys(sources))
    return dict(base_url=base,newsnow_sources=sources,
        max_items=_number(config,'max_items',1200,1,10000,integer=True),
        newsnow_connect_timeout=_number(config,'newsnow_connect_timeout',5,.1,60),
        newsnow_read_timeout=_number(config,'newsnow_read_timeout',15,.1,120),
        newsnow_total_timeout=_number(config,'newsnow_total_timeout',180,.1,600),
        newsnow_max_response_bytes=_number(config,'newsnow_max_response_bytes',2*1024*1024,1024,2*1024*1024,integer=True))


def parse_time(value):
    """Parse source timestamps; absent/invalid stays unknown, including dates.

    Date-only strings are normalized to local midnight for existing storage,
    with date precision retained so the UI must not claim an observed clock.
    """
    empty=dict(value='',precision='unknown',timezone_assumed=False)
    if value is None or isinstance(value,bool) or isinstance(value,(dict,list)):return empty
    dt=None;precision='second';assumed=False
    try:
        if isinstance(value,(int,float)) or (isinstance(value,str) and re.fullmatch(r'\d+(?:\.\d+)?',value.strip())):
            timestamp=float(value)
            if not math.isfinite(timestamp) or timestamp<=0:return empty
            if timestamp>=1e11:timestamp/=1000
            dt=datetime.fromtimestamp(timestamp,timezone.utc)
        elif isinstance(value,str):
            sample=value.strip()
            if not sample:return empty
            match=_DATE_RE.fullmatch(sample)
            if match:
                y,mo,d,hh,minute,sec,micro=match.groups()
                precision='second' if hh is not None else 'date'
                dt=datetime(int(y),int(mo),int(d),int(hh or 0),int(minute or 0),int(sec or 0),
                    int((micro or '').ljust(6,'0') or 0),tzinfo=CN_TZ)
                assumed=True
            else:
                try:dt=datetime.fromisoformat(sample.replace('Z','+00:00'))
                except ValueError:dt=parsedate_to_datetime(sample)
                if dt.tzinfo is None:dt=dt.replace(tzinfo=CN_TZ);assumed=True
        if dt is None or not 1970<=dt.year<=2200:return empty
        return dict(value=_iso(dt),precision=precision,timezone_assumed=assumed)
    except (ValueError,TypeError,OverflowError,OSError):return empty


def _to_iso(value):
    return parse_time(value)['value']


def _item_time(raw):
    extra=raw.get('extra') if isinstance(raw.get('extra'),dict) else {}
    for field,value in (('pubDate',raw.get('pubDate')),('extra.date',extra.get('date')),
                        ('date',raw.get('date')),('published_at',raw.get('published_at'))):
        parsed=parse_time(value)
        if parsed['value']:return dict(parsed,field=field)
    return dict(value='',precision='unknown',timezone_assumed=False,field='')


def _canonical_bytes(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')


def _to_item(source_id,raw,position,snapshot,observed):
    if not isinstance(raw,dict):return None
    title=raw.get('title')
    if not isinstance(title,str) or not title.strip():return None
    title=title.strip()
    uri=raw.get('url') or raw.get('mobileUrl')
    try:uri=_url(uri)
    except ValueError:return None
    extra=raw.get('extra') if isinstance(raw.get('extra'),dict) else {}
    content=extra.get('hover') if isinstance(extra.get('hover'),str) else ''
    publication=_item_time(raw)
    spec=SOURCE_CATALOG.get(source_id,{})
    native_id=raw.get('id')
    if isinstance(native_id,(dict,list,bool)) or native_id is None:native_id=''
    native_id=str(native_id)
    fallback_id=hashlib.sha256(uri.encode('utf-8')).hexdigest()[:24]
    metrics={key:raw[key] for key in ('hot_value','heat_score','score','rank','position','view','like','comment','share') if key in raw}
    metadata=dict(source_id=source_id,source_name=SOURCE_NAMES.get(source_id,source_id),
        source_type=spec.get('kind','unknown'),is_news=spec.get('is_news',True),
        item_id=native_id,observed_list_position=position,position_kind='returned_list_position',
        snapshot_reported_at=snapshot['snapshot_reported_at'],snapshot_time_basis='newsnow_reported_updatedTime',
        observed_at=observed,upstream_status=snapshot['response_status'],
        snapshot_sha256=snapshot['raw_sha256'],raw_metrics=metrics,
        response_source_id=snapshot.get('response_source_id',source_id),
        heat_text=extra.get('info') if isinstance(extra.get('info'),str) else '',
        publication_time_field=publication['field'],publication_precision=publication['precision'],
        publication_timezone_assumed=publication['timezone_assumed'],
        content_kind='source_excerpt' if content.strip() else 'title_only',requires_original_text=True,
        title_truncated=len(title)>2000,excerpt_truncated=len(content)>20000)
    return dict(id='newsnow:'+source_id+':'+(native_id or fallback_id),title=title[:2000],url=uri,
        content=content[:20000].strip(),published_at=publication['value'],fetched_at=observed,
        source=dict(name=SOURCE_NAMES.get(source_id,source_id),domain=(urlsplit(uri).hostname or '').lower().rstrip('.')),
        image='',newsnow_source=source_id,newsnow_extra=extra,newsnow=metadata,
        material_kind='newsnow_discovery',content_kind=metadata['content_kind'],requires_original_text=True)


def _read_body(response,limit,deadline,check_cancel):
    try:size=int(response.headers.get('content-length','0'))
    except (TypeError,ValueError):size=0
    if size>limit:raise _SourceFailure('response_too_large','返回内容超过单源大小限制。')
    chunks=[];size=0
    for chunk in response.iter_content(chunk_size=65536):
        if check_cancel:check_cancel()
        if monotonic()>=deadline:raise _SourceFailure('budget_exceeded','本轮请求时间预算已用尽。')
        if not chunk:continue
        size+=len(chunk)
        if size>limit:raise _SourceFailure('response_too_large','返回内容超过单源大小限制。')
        chunks.append(chunk)
    return b''.join(chunks)


def _finite_float(value):
    number=float(value)
    if not math.isfinite(number):raise ValueError('non-finite JSON number')
    return number


def _decode_response(response,blob):
    mime=response.headers.get('content-type','').split(';',1)[0].strip().lower()
    if mime!='application/json' and not mime.endswith('+json'):
        raise _SourceFailure('not_json','返回内容不是JSON，可能是页面回退或服务配置有误。')
    try:
        payload=json.loads(blob.decode('utf-8-sig'),parse_float=_finite_float,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except (ValueError,UnicodeError,RecursionError):raise _SourceFailure('invalid_json','返回内容不是有效的UTF-8 JSON。') from None
    return payload


def _source_report(source_id):
    spec=SOURCE_CATALOG.get(source_id,{})
    return dict(source_id=source_id,name=SOURCE_NAMES.get(source_id,source_id),kind=spec.get('kind','unknown'),
        status='not_attempted',http_status=None,raw_items=0,valid_items=0,selected_items=0,invalid_items=0,
        error='',error_code='',observed_at='',snapshot_reported_at='',upstream_status='',
        payload_sha256='',freshness='unknown')


def fetch_batch(config,check_cancel=None):
    """Fetch every selected source, then choose online items round-robin.

    The raw snapshots retain all response items, including invalid/non-news and
    items beyond max_items. No database connection is used here.
    """
    options=validate(config)
    deadline=monotonic()+options['newsnow_total_timeout']
    reports=[];snapshots=[];groups=[];batch_bytes=0
    for source_id in options['newsnow_sources']:
        if check_cancel:check_cancel()
        row=_source_report(source_id);reports.append(row)
        remaining=deadline-monotonic()
        if remaining<=0:
            row.update(error_code='budget_exceeded',error='本轮请求时间预算已用尽，尚未请求此源。')
            groups.append(deque());continue
        byte_budget=MAX_BATCH_RESPONSE_BYTES-batch_bytes
        if byte_budget<=0:
            row.update(error_code='batch_size_exceeded',error='本轮响应总量已达32MiB，尚未请求此源。')
            groups.append(deque());continue
        response=None;snapshot=None
        try:
            row['observed_at']=_now()
            response=requests.get(options['base_url']+'/api/s',params={'id':source_id},
                headers={'Accept':'application/json'},timeout=(min(options['newsnow_connect_timeout'],remaining),
                min(options['newsnow_read_timeout'],remaining)),stream=True,allow_redirects=False)
            row['observed_at']=_now();row['http_status']=response.status_code
            blob=_read_body(response,min(options['newsnow_max_response_bytes'],byte_budget),deadline,check_cancel)
            batch_bytes+=len(blob)
            snapshot=dict(source_id=source_id,observed_at=row['observed_at'],response_status='',
                snapshot_reported_at='',snapshot_time_basis='newsnow_reported_updatedTime',
                payload=None,payload_sha256='',valid_response=False,
                raw_sha256=hashlib.sha256(blob).hexdigest(),raw_body_b64=base64.b64encode(blob).decode('ascii'))
            snapshots.append(snapshot)
            if response.status_code!=200:
                raise _SourceFailure('http_error','NewsNow返回HTTP '+str(response.status_code)+'，请核对来源与服务状态。')
            payload=_decode_response(response,blob)
            snapshot.update(payload=payload,payload_sha256=hashlib.sha256(_canonical_bytes(payload)).hexdigest())
            row['payload_sha256']=snapshot['payload_sha256']
            if not isinstance(payload,dict) or payload.get('error') or not isinstance(payload.get('items'),list):
                raise _SourceFailure('invalid_payload','JSON响应缺少有效items数组，不能视为采集成功。')
            if payload.get('id') not in (source_id,SOURCE_ALIASES.get(source_id,source_id)):
                raise _SourceFailure('source_mismatch','JSON响应的来源编号与请求不一致。')
            if payload.get('status') not in ('success','cache'):
                raise _SourceFailure('invalid_status','JSON响应的上游状态无法识别。')
            reported=_to_iso(payload.get('updatedTime'))
            if not reported:raise _SourceFailure('invalid_snapshot_time','JSON响应缺少有效快照时间。')
            snapshot.update(response_status=payload['status'],snapshot_reported_at=reported,valid_response=True,
                response_source_id=payload['id'])
            row['response_source_id']=payload['id']
            normalized=[]
            for position,raw in enumerate(payload['items'],1):
                if check_cancel:check_cancel()
                if monotonic()>=deadline:raise _SourceFailure('budget_exceeded','本轮请求时间预算已用尽。')
                item=_to_item(source_id,raw,position,snapshot,row['observed_at'])
                if item is not None:normalized.append(item)
            row.update(status='success' if payload['items'] else 'empty',raw_items=len(payload['items']),
                valid_items=len(normalized),invalid_items=len(payload['items'])-len(normalized),
                snapshot_reported_at=reported,upstream_status=payload['status'],payload_sha256=snapshot['payload_sha256'])
            if payload['items'] and not normalized:
                row.update(status='failed',error_code='invalid_items',error='来源有返回条目，但没有有效标题与网页链接。')
            groups.append(deque(normalized))
        except _SourceFailure as exc:
            if snapshot is not None:snapshot.update(valid_response=False,error_code=exc.code)
            row.update(status='failed',error_code=exc.code,error=str(exc));groups.append(deque())
        except requests.RequestException:
            row.update(status='failed',error_code='request_failed',error='无法读取NewsNow来源，请检查服务连接或稍后重试。')
            groups.append(deque())
        finally:
            if response is not None:response.close()
    selected=[]
    while len(selected)<options['max_items'] and any(groups):
        for row,group in zip(reports,groups):
            if len(selected)>=options['max_items']:break
            if group:selected.append(group.popleft());row['selected_items']+=1
    succeeded=sum(row['status'] in ('success','empty') for row in reports)
    failed=len(reports)-succeeded
    valid=sum(row['valid_items'] for row in reports)
    result=dict(items=selected,sources=reports,snapshots=snapshots,
        summary=dict(requested_sources=len(reports),succeeded_sources=succeeded,failed_sources=failed,
            empty_sources=sum(row['status']=='empty' for row in reports),raw_items=sum(row['raw_items'] for row in reports),
            valid_items=valid,selected_items=len(selected),truncated=valid>len(selected),
            total_response_bytes=batch_bytes,
            status='failed' if not succeeded else 'partial' if failed else 'empty' if not selected else 'complete'))
    if not succeeded:raise NewsNowUnavailable(result)
    return result


def fetch_news(config):
    return fetch_batch(config)['items']
