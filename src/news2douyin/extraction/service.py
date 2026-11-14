"""Durable revision queue. Model work runs outside SQLite transactions."""
import json
import os
import time
from uuid import uuid4
from sqlalchemy import delete, or_
from sqlmodel import Session, select
from ..storage.articles import lock_news
from ..storage.models import ArticleVersion, ArticleEventLink
from ..desk.models import DeskDirty
from . import reader
from .models import VersionReading, TextMention, MentionReview, ExtractionAttempt
from .schema import cluster_key

VERSION = '1.0'
NEW = 'baseline'
MODEL = 'model_queued'


def initialize(engine,*,backfill=True,checkpoint=False):
    """Always install enqueue triggers; optionally checkpoint startup backfill.

    A normal restart must not aggregate all article versions again. The marker
    shares the DDL/backfill transaction and includes the reader mode, so a
    failed upgrade retries and a rules/model configuration change still queues
    the relevant historical versions. Direct maintenance calls keep their
    previous uncheckpointed behavior unless explicitly opted in.
    """
    from ..storage.sql import install_sql_functions
    from sqlalchemy import text
    reader_mode = 'model' if os.getenv('NEWS2DOUYIN_EVENT_READER','rules')=='model' else 'rules'
    marker = 'extraction_startup_backfill_1'
    generation = VERSION + ':' + reader_mode
    with engine.begin() as db:
        install_sql_functions(db)
        if checkpoint and backfill:
            previous = db.execute(text('SELECT value FROM deskmeta WHERE key=:key').execution_options(
                n2d_query_name='startup.extraction_checkpoint'), {'key': marker}).scalar()
            backfill = previous != generation
        if engine.dialect.name=='postgresql':
            db.exec_driver_sql("""CREATE OR REPLACE FUNCTION n2d_enqueue_extraction()
                RETURNS trigger LANGUAGE plpgsql AS $n2d$
                BEGIN
                  INSERT INTO versionreading(version_id,state,lease,"until",method,model,note)
                  VALUES(NEW.id,'baseline','',0,'','','') ON CONFLICT(version_id) DO NOTHING;
                  RETURN NEW;
                END $n2d$""")
            db.exec_driver_sql('CREATE OR REPLACE TRIGGER extraction_version AFTER INSERT ON articleversion FOR EACH ROW EXECUTE FUNCTION n2d_enqueue_extraction()')
            if backfill:
                db.exec_driver_sql("""INSERT INTO versionreading(version_id,state,lease,"until",method,model,note)
                    SELECT max(id),'baseline','',0,'','','' FROM articleversion GROUP BY article_key
                    ON CONFLICT(version_id) DO NOTHING""")
        else:
            db.exec_driver_sql("""CREATE TRIGGER IF NOT EXISTS extraction_version AFTER INSERT ON articleversion BEGIN
              INSERT OR IGNORE INTO versionreading(version_id,state,lease,"until",method,model,note)
              VALUES(NEW.id,'baseline','',0,'','',''); END""")
            if backfill:
                db.exec_driver_sql("""INSERT OR IGNORE INTO versionreading(version_id,state,lease,"until",method,model,note)
                    SELECT max(id),'baseline','',0,'','','' FROM articleversion GROUP BY article_key""")
        if backfill and reader_mode=='model':
            db.exec_driver_sql("""UPDATE versionreading SET state='model_queued'
                WHERE state='ready' AND method='rules' AND model=''
                AND version_id IN (SELECT max(id) FROM articleversion GROUP BY article_key)""")
        if checkpoint and backfill:
            db.execute(text('''INSERT INTO deskmeta(key,value) VALUES (:key,:value)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value''').execution_options(
                    n2d_query_name='startup.extraction_checkpoint_publish'),
                {'key': marker, 'value': generation})


def schedule_model(session, version):
    lock_news(session,'extract-version/'+str(version.id))
    job = session.exec(select(VersionReading).where(VersionReading.version_id==version.id).with_for_update()).first()
    if not job:
        job = VersionReading(version_id=version.id,state=NEW)
    if job.state == NEW or (job.state=='rule_running'):
        job.note = 'model_after_baseline'
    elif job.state != 'model_running':
        job.state = MODEL
    session.add(job)
    session.commit()
    return job.state


def pick(engine, model):
    now = time.time()
    queued = MODEL if model else NEW
    running = 'model_running' if model else 'rule_running'
    query = select(VersionReading).where(or_(VersionReading.state==queued,
        (VersionReading.state==running) & (VersionReading.until < now),
        (VersionReading.state==('model_wait' if model else 'rule_wait')) & (VersionReading.until < now))).order_by(VersionReading.version_id).limit(1)
    with Session(engine, expire_on_commit=False) as s:
        if engine.dialect.name=='postgresql':
            # Row claims are committed before any model call; other workers skip them.
            query=query.with_for_update(skip_locked=True)
        else:
            candidate=s.exec(query).first()
            if candidate is None:return None
            ident=candidate.version_id
            s.rollback()
            lock_news(s,'extract-version/'+str(ident))
        job = s.exec(query).first()
        if not job:
            return None
        version = s.get(ArticleVersion,job.version_id)
        if version is None:
            s.delete(job)
            s.commit()
            return None
        job.state, job.lease, job.until = running, uuid4().hex, now+180
        s.add(job)
        s.commit()
        return version, job.lease


def touch_events(s, article):
    from ..desk.service import enqueue_dirty
    for key in s.exec(select(ArticleEventLink.event_key).where(ArticleEventLink.article_key==article)):
        enqueue_dirty(s,key)


def step(engine, model=False):
    chosen = pick(engine, model)
    if not chosen:
        return False
    version, lease = chosen
    method = 'model' if model else 'rules'
    model_name = ''
    error = ''
    try:
        if model:
            from ..llm.settings import get_settings
            model_name = get_settings().model
        items, rejected = reader.extract(version, json.loads(version.payload_json), method)
    except Exception as exc:
        # Do not expose credentials, endpoint bodies or source text in task messages.
        error = type(exc).__name__
    with Session(engine) as s:
        lock_news(s,'extract-version/'+str(version.id))
        job = s.exec(select(VersionReading).where(VersionReading.version_id==version.id).with_for_update()).first()
        if not job or job.lease != lease:
            return True
        wants_model = job.note=='model_after_baseline' or os.getenv('NEWS2DOUYIN_EVENT_READER','rules')=='model'
        job.until, job.lease = 0.0, ''
        if error:
            attempt=s.get(ExtractionAttempt,version.id) or ExtractionAttempt(version_id=version.id)
            attempt.attempts+=1;attempt.method=method;s.add(attempt)
            job.state='model_wait' if model else 'rule_wait'
            job.until=time.time()+min(60*2**(attempt.attempts-1),3600)
            if attempt.attempts>=4 or error not in ('TimeoutError','URLError','HTTPError','ConnectionError','ReadTimeout','ConnectTimeout'):
                job.state='error'
            job.note='抽取失败，'+('等待后台重试' if job.state!='error' else '可手动重试')+' · '+error
        else:
            attempt=s.get(ExtractionAttempt,version.id)
            if attempt:s.delete(attempt)
            if model and not items and s.exec(select(TextMention.id).where(
                    TextMention.version_id==version.id).limit(1)).first():
                job.state,job.note='ready','模型未返回陈述，保留已有抽取'
                job.model=model_name
                s.add(job)
                s.commit()
                return True
            s.exec(delete(TextMention).where(TextMention.version_id==version.id))
            for row in items:
                s.add(TextMention(id=row['id'],version_id=version.id,article=version.article_key,
                    revision=version.revision,action_date=row['date'],signature=cluster_key(row),data=json.dumps(row,ensure_ascii=False)))
            job.method, job.model = method, model_name
            job.state = MODEL if not model and wants_model else 'ready'
            job.note = f'{len(items)} 条陈述，拒绝 {rejected} 条无依据输出'
            touch_events(s, version.article_key)
        s.add(job)
        s.commit()
    return True


def mentions(s, version_id, include_rejected=False):
    stmt = select(TextMention,MentionReview).outerjoin(MentionReview,MentionReview.id==TextMention.id
        ).where(TextMention.version_id==version_id).order_by(TextMention.id)
    rows = []
    for item, choice in s.exec(stmt):
        decision = choice.decision if choice else 'pending'
        if include_rejected or decision!='rejected':
            rows.append(dict(json.loads(item.data), decision=decision))
    return sorted(rows,key=lambda x:(x['field']!='title',x['start'],x['quote'].find(x['trigger'])))


def article_view(s, version):
    job = s.get(VersionReading, version.id)
    return dict(state=job.state if job else NEW, method=job.method if job else '',
                model=job.model if job else '',note=job.note if job and job.note!='model_after_baseline' else '',
                items=mentions(s,version.id,True),revision=version.revision)


def decide(s, mention, decision):
    candidate=s.get(TextMention,mention)
    if not candidate:raise KeyError('陈述不存在或已更新')
    lock_news(s,'extract-version/'+str(candidate.version_id))
    row = s.exec(select(TextMention).where(TextMention.id==mention)).first()
    if not row:
        raise KeyError('陈述不存在或已更新')
    if decision not in ('accepted','rejected','pending'):
        raise ValueError('无效核对状态')
    s.merge(MentionReview(id=mention,decision=decision))
    touch_events(s,row.article)
    s.commit()


def neighbors(s,key):
    item = s.get(TextMention,key)
    if not item:
        raise KeyError('陈述不存在')
    choice = s.get(MentionReview,key)
    if not item.signature or (choice and choice.decision=='rejected'):
        return []
    # Historical versions remain readable; only current versions are eligible as peers.
    from sqlalchemy import func
    latest = select(func.max(ArticleVersion.id)).group_by(ArticleVersion.article_key)
    pairs = s.exec(select(TextMention).outerjoin(MentionReview,MentionReview.id==TextMention.id)
        .where(TextMention.signature==item.signature,TextMention.article!=item.article,
               TextMention.version_id.in_(latest),or_(MentionReview.id==None,MentionReview.decision!='rejected'))
        .order_by(TextMention.article,TextMention.id).limit(30)).all()
    return [dict(article=p.article,revision=p.revision,quote=json.loads(p.data)['quote']) for p in pairs]

def focus_timeline(s, watch):
    from sqlalchemy import func
    from ..desk.models import WatchEvent
    from ..desk.service import current_membership
    members=select(ArticleEventLink.article_key).join(WatchEvent,WatchEvent.event==ArticleEventLink.event_key
        ).where(WatchEvent.watch==watch,WatchEvent.choice!='exclude',current_membership())
    latest=select(func.max(ArticleVersion.id)).where(ArticleVersion.article_key.in_(members)).group_by(ArticleVersion.article_key)
    stmt=select(TextMention).outerjoin(MentionReview,MentionReview.id==TextMention.id).where(
        TextMention.version_id.in_(latest),TextMention.action_date!='',
        or_(MentionReview.id==None,MentionReview.decision!='rejected')).order_by(
        TextMention.action_date.desc(),TextMention.id).limit(60)
    return [json.loads(row.data) for row in s.exec(stmt)]
