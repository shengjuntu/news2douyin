"""Strict source parsing of GDELT 2.0's 61 Event and 16 Mentions columns.

Codes and source IDs remain strings. Geography country codes are explicitly
FIPS (action geography) or CAMEO/ISO-like actor codes, not guessed ISO alpha-2.
Original bytes are retained by the lake regardless of parse validity.
"""
import base64
from datetime import datetime, timezone
from functools import lru_cache
import json
import math
from pathlib import Path
import re
from urllib.parse import urlsplit
from .feeds import kind_for


@lru_cache(maxsize=1)
def _actor_countries():
    rows=json.loads((Path(__file__).resolve().parents[1]/'server/data/countries.json').read_text(encoding='utf-8'))
    return {row['a3']: row['a2'].lower() for row in rows}


def _time(value, day=False):
    pattern='%Y%m%d' if day else '%Y%m%d%H%M%S'
    if not re.fullmatch(r'\d{8}' if day else r'\d{14}', value): raise ValueError('invalid_source_date')
    moment=datetime.strptime(value,pattern).replace(tzinfo=timezone.utc)
    return moment.strftime('%Y-%m-%d') if day else moment.isoformat(timespec='seconds').replace('+00:00','Z')


def _int(value, minimum=0, maximum=None):
    if value=='': return None
    if not re.fullmatch(r'-?\d+',value): raise ValueError('invalid_integer')
    result=int(value)
    if result<minimum or (maximum is not None and result>maximum): raise ValueError('integer_out_of_range')
    return result


def _float(value, minimum=None, maximum=None):
    if value=='': return None
    result=float(value)
    if not math.isfinite(result) or (minimum is not None and result<minimum) or (maximum is not None and result>maximum):
        raise ValueError('number_out_of_range')
    return result


def document_domain(value):
    try:
        parsed=urlsplit(value)
        if parsed.scheme.lower() not in ('http','https') or parsed.username or parsed.password or not parsed.hostname:
            return ''
        parsed.port
        return parsed.hostname.lower()
    except ValueError: return ''


def _event(c):
    if not re.fullmatch(r'\d+',c[0]): raise ValueError('invalid_event_id')
    for value, minimum, maximum in ((c[26],2,4),(c[27],2,3),(c[28],2,2)):
        if not re.fullmatch(r'\d{'+str(minimum)+','+str(maximum)+'}',value): raise ValueError('invalid_cameo_code')
    from ..collect.providers.gkg import FIPS
    actor_map=_actor_countries()
    return dict(event_id=c[0], event_date=_time(c[1],day=True), added_at=_time(c[59]),
        actor1_name=c[6],actor2_name=c[16],actor1_country=c[7],actor2_country=c[17],
        actor1_country_iso2=actor_map.get(c[7],''),actor2_country_iso2=actor_map.get(c[17],''),
        is_root=bool(_int(c[25],0,1)),event_code=c[26],event_base_code=c[27],event_root_code=c[28],
        quad_class=_int(c[29],1,4),goldstein=_float(c[30],-10,10),
        initial_mentions=_int(c[31]),initial_sources=_int(c[32]),initial_articles=_int(c[33]),avg_tone=_float(c[34]),
        action_place=c[52],action_country=c[53],action_country_iso2=FIPS.get(c[53].upper(),''),
        latitude=_float(c[56],-90,90),longitude=_float(c[57],-180,180),source_url=c[60])


def _mention(c):
    if not re.fullmatch(r'\d+',c[0]): raise ValueError('invalid_event_id')
    if not c[5]: raise ValueError('missing_document_identifier')
    return dict(event_id=c[0],event_time=_time(c[1]),mention_time=_time(c[2]),
        mention_type=_int(c[3],1),source_name=c[4],doc_identifier=c[5],domain=document_domain(c[5]),
        sentence_id=_int(c[6]),actor1_offset=_int(c[7],-1),actor2_offset=_int(c[8],-1),action_offset=_int(c[9],-1),
        in_raw_text=None if c[10]=='' else bool(_int(c[10],0,1)),confidence=_int(c[11],0,100),
        doc_length=_int(c[12]),doc_tone=_float(c[13]))


def decoded_row(raw,feed,stamp,index,observed):
    kind=kind_for(feed)
    if kind not in ('events','mentions'): raise ValueError('not_official_event_feed')
    metadata={};error=''
    try: line=raw.decode('utf-8')
    except UnicodeDecodeError:
        line=raw.decode('utf-8','replace');error='invalid_utf8'
        metadata['raw_bytes_base64']=base64.b64encode(raw).decode('ascii')
    line=line.rstrip('\r\n');columns=line.split('\t')
    count=61 if kind=='events' else 16
    if len(columns)!=count: error=error or 'expected_'+str(count)+'_columns'
    else:
        try: metadata.update(_event(columns) if kind=='events' else _mention(columns))
        except (ValueError,OverflowError) as exc: error=error or ('invalid_official_record:'+str(exc)[:60])
    metadata['source_kind']=kind
    rid=columns[0] if columns else ''
    date=(columns[1] if len(columns)>1 else '')
    url=metadata.get('source_url',metadata.get('doc_identifier',''))
    title=(metadata.get('actor1_name','')+' → '+metadata.get('actor2_name','')).strip(' →') if kind=='events' else ''
    return dict(feed=feed,stamp=stamp,row_index=index,record_id=rid,document_date=date,url=url,title=title,
        metadata=metadata,valid=not error,error=error,raw_line=line,observed_at=observed)
