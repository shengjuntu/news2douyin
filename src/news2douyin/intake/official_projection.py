"""Compact working index of official source events and distinct source documents.

Raw Mentions rows remain in the lake, including repeated sentences and update
observations. This index deliberately has one row per (GlobalEventID, document)
so repeated updates do not inflate document/source counts. It does not create
local Event/RadarEntry/research objects. The caller commits receipts and offsets
in the same transaction and owns the per-batch lock.
"""
import hashlib
import json
from sqlalchemy import func, update
from sqlmodel import select
from .feeds import kind_for
from .official_models import OfficialEvent, OfficialMention
from ..storage.models import utc_now_iso
from ..storage.articles import lock_news


def mention_key(event_id, document):
    return hashlib.sha256(json.dumps([event_id,document],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def _ref(raw):
    return dict(feed=raw['feed'],stamp=raw['stamp'],row_index=int(raw['row_index']))


def _reference_order(raw):
    return raw['stamp'],raw['feed'],int(raw['row_index'])


def _event(session, raw, meta):
    key=meta['event_id']
    lock_news(session,'official-event/'+key)
    row=session.get(OfficialEvent,key)
    fields={name:meta.get(name) for name in OfficialEvent.model_fields
            if name in meta and name not in ('feed','stamp','row_index','first_seen','last_seen','ingested_at')}
    if row is None:
        latest=session.exec(select(func.max(OfficialMention.last_seen)).where(OfficialMention.event_id==key)).one()
        fields.update(_ref(raw),first_seen=meta['added_at'],last_seen=max(meta['added_at'],latest or ''),
                      ingested_at=raw.get('observed_at') or utc_now_iso())
        session.add(OfficialEvent(**fields))
    elif _reference_order(raw)>(row.stamp,row.feed,row.row_index):
        for field,value in fields.items():setattr(row,field,value)
        row.feed,row.stamp,row.row_index=raw['feed'],raw['stamp'],int(raw['row_index'])
        row.first_seen=min(row.first_seen or meta['added_at'],meta['added_at'])
        row.last_seen=max(row.last_seen,meta['added_at'])
        row.ingested_at=raw.get('observed_at') or utc_now_iso()
        session.add(row)
    return key


def _mention(session, raw, meta):
    key=mention_key(meta['event_id'],meta['doc_identifier'])
    # Every producer locks event before mention, matching official event inserts
    # and avoiding reverse-order deadlocks between the two source streams.
    lock_news(session,'official-event/'+meta['event_id'],'official-mention/'+key)
    row=session.get(OfficialMention,key)
    when=meta['mention_time']
    values={name:meta.get(name) for name in ('event_id','doc_identifier','domain','source_name','mention_type',
        'event_time','confidence','sentence_id','actor1_offset','actor2_offset','action_offset',
        'in_raw_text','doc_length','doc_tone')}
    if row is None:
        values.update(key=key,first_seen=when,last_seen=when,first_feed=raw['feed'],first_stamp=raw['stamp'],
            first_row_index=int(raw['row_index']),last_feed=raw['feed'],last_stamp=raw['stamp'],
            last_row_index=int(raw['row_index']),updated_at=raw.get('observed_at') or utc_now_iso())
        session.add(OfficialMention(**values))
    else:
        if row.event_id!=meta['event_id'] or row.doc_identifier!=meta['doc_identifier']:
            raise ValueError('official_mention_identity_collision')
        first_order=(when,*_reference_order(raw))
        old_first=(row.first_seen,row.first_stamp,row.first_feed,row.first_row_index)
        old_last=(row.last_seen,row.last_stamp,row.last_feed,row.last_row_index)
        if first_order<old_first:
            row.first_seen,row.first_feed,row.first_stamp,row.first_row_index=when,raw['feed'],raw['stamp'],int(raw['row_index'])
        if first_order>old_last:
            for field,value in values.items():setattr(row,field,value)
            row.last_seen,row.last_feed,row.last_stamp,row.last_row_index=when,raw['feed'],raw['stamp'],int(raw['row_index'])
            row.updated_at=raw.get('observed_at') or utc_now_iso()
        session.add(row)
    # Do not create a stub event from a mention; events can arrive out of order.
    # SQL flush before this update is local, never spans a remote operation.
    session.flush()
    session.exec(update(OfficialEvent).where(OfficialEvent.event_id==meta['event_id'],
        OfficialEvent.last_seen<when).values(last_seen=when).execution_options(synchronize_session=False,
            n2d_query_name='official.mention.touch_event'))
    return meta['event_id']


def project_row(session, raw):
    kind=kind_for(raw['feed'])
    if kind not in ('events','mentions'):raise ValueError('not_official_event_feed')
    if not raw.get('valid'):
        return dict(status='skipped',event_id='',error=raw.get('error') or 'invalid_official_record')
    meta=raw.get('metadata') or {}
    if isinstance(meta,str):meta=json.loads(meta)
    key=_event(session,raw,meta) if kind=='events' else _mention(session,raw,meta)
    return dict(status='projected',event_id=key,error='')
