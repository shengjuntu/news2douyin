"""Read one GDELT GKG 2.1 quarter-hour ZIP from the original-language stream."""
from datetime import datetime, timedelta, timezone
from html import unescape
import io
import re
from zipfile import ZipFile, BadZipFile
from zoneinfo import ZoneInfo
import requests
from .gdelt import _article, COUNTRIES

FIPS={info[0].upper():iso for iso,info in COUNTRIES.items()}
from ...search.dates import calendar_day, timezone_name
from ...storage.models import utc_now_iso

BASE='https://data.gdeltproject.org/gdeltv2/'
MAX_ZIP=48*1024*1024
MAX_TEXT=192*1024*1024

def validate(config):
    if config.get('country') or config.get('language'):
        raise ValueError('GKG 不按媒体国家或语言筛选，请在检索词中填写关注对象')
    q=config.get('search_query','')
    if not isinstance(q,str) or not q.strip() or len(q)>100 or not 1<=int(config.get('max_items',100))<=250:
        raise ValueError('GKG 需要 1–100 字符检索词（* 表示全部），单批 1–250 条')
    if config.get('collection_mode','search')!='search':raise ValueError('GKG 仅支持批次检索')
    clock=config.get('gkg_time','')
    if clock and (not isinstance(clock,str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):(?:00|15|30|45)',clock)):
        raise ValueError('GKG 时间须为整刻，例如 12:15')

def batch(config,now=None):
    validate(config)
    zone=ZoneInfo(timezone_name(config.get('timezone','')))
    now=(now or datetime.now(timezone.utc)).astimezone(zone)
    day=calendar_day(config.get('date_str',''),str(zone))
    if config.get('gkg_time'):
        selected=datetime.fromisoformat(day+'T'+config['gkg_time']).replace(tzinfo=zone)
    elif day==now.date().isoformat():
        selected=now-timedelta(minutes=30)
    else:selected=datetime.fromisoformat(day+'T23:45').replace(tzinfo=zone)
    if selected.date().isoformat()!=day or selected>now:
        raise ValueError('选择的 GKG 批次尚未发布，请调整采集日期或批次时间')
    selected=selected.astimezone(timezone.utc)
    return selected.replace(minute=(selected.minute//15)*15,second=0,microsecond=0).strftime('%Y%m%d%H%M%S')

def scheduled_batch(config, occurrence):
    """Use the planned occurrence even if queue submission was delayed."""
    if config.get('gkg_time'):
        return batch(config)
    validate(config)
    ready = occurrence.astimezone(timezone.utc) - timedelta(minutes=30)
    ready = ready.replace(minute=(ready.minute // 15) * 15, second=0, microsecond=0)
    return ready.strftime('%Y%m%d%H%M%S')


def parse_archive(blob,stamp,config):
    validate(config)
    max_items=int(config.get('max_items',100))
    if not re.fullmatch(r'\d{12}00',stamp) or len(blob)>MAX_ZIP:raise ValueError('GKG 批次或大小无效')
    tokens=[s.strip().casefold() for s in re.split('[，,]',config['search_query']) if s.strip()]
    if not tokens:raise ValueError('请输入有效检索词')
    found=[]; seen=set(); fetched=utc_now_iso()
    with ZipFile(io.BytesIO(blob)) as archive:
        members=archive.infolist()
        if len(members)!=1 or members[0].filename!=stamp+'.gkg.csv' or members[0].file_size>MAX_TEXT:
            raise ValueError('GKG 归档格式或展开大小不符合预期')
        with archive.open(members[0]) as stream:
            for line in stream:
                if len(line)>4*1024*1024:continue
                c=line.decode('utf8','replace').rstrip('\r\n').split('\t')
                if len(c)!=27 or c[1]!=stamp or not re.fullmatch(stamp+r'-\d+',c[0]):continue
                def extra(tag):
                    m=re.search('<'+tag+'>(.*?)</'+tag+'>',c[26],re.S)
                    return unescape(m[1].strip()) if m else ''
                title=extra('PAGE_TITLE')[:1500]
                if not title or c[4] in seen:continue
                haystack=' '.join([title,c[7],c[9],c[11],c[13]]).casefold()
                if '*' not in tokens and not any(t in haystack for t in tokens):continue
                item=_article(dict(url=c[4],title=title,seendate=stamp[:8]+'T'+stamp[8:]+'Z'),fetched)
                if not item:continue
                def split(col):return list(dict.fromkeys(t.strip()[:160] for t in c[col].split(';') if t.strip()))[:100]
                locs=[r.split('#')[1] for r in c[9].split(';') if '#' in r]
                item['gkg']={'record_id':c[0],'persons':split(11),'orgs':split(13),
                     'themes':split(7),'mention_iso':sorted({FIPS[x.split('#')[2]] for x in c[9].split(';') if len(x.split('#'))>2 and x.split('#')[2] in FIPS}),'locations':list(dict.fromkeys(locs))[:80],'author':extra('PAGE_AUTHORS')[:300]}
                seen.add(c[4]);found.append(item)
                if len(found)>=max_items:break
    return found

def fetch_news(config):
    stamp=config.get('gkg_batch') or batch(config)
    if not re.fullmatch(r'[0-9]{14}',stamp):raise ValueError('无效 GKG 批次编号')
    try:
        with requests.get(BASE+stamp+'.gkg.csv.zip',stream=True,timeout=(6,35),allow_redirects=False) as response:
            if response.status_code!=200:raise RuntimeError(f'GKG 批次 {stamp} HTTP {response.status_code}')
            data=bytearray()
            for chunk in response.iter_content(131072):
                if len(chunk)+len(data)>MAX_ZIP:raise ValueError('GKG 归档超过大小限制')
                data.extend(chunk)
        return parse_archive(data,stamp,config)
    except (requests.RequestException,BadZipFile) as exc:
        raise RuntimeError(f'GKG 批次 {stamp} 下载或解包失败') from exc
