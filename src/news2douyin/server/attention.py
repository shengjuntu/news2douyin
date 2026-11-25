"""Attention radar APIs. Reads touch only bounded operational SQL tables."""
from fastapi import Depends, Query
from pydantic import BaseModel,ConfigDict,Field
from sqlmodel import Session,select
from .research import same_origin
from .tasks import task_call
from .gdelt_events import SelectCandidate,display_time
from ..attention import service
from ..storage.models import RunRecord
from ..storage.utils import loads


RECENT_RUN_LIMIT = 100
SOURCE_STATES = ('success', 'empty', 'failed', 'not_attempted')


def recent_collection(session):
    """Read stored diagnostics without making a request to NewsNow."""
    rows = session.exec(
        select(RunRecord.id, RunRecord.stats_json)
        .where(RunRecord.trigger_type != 'profile_test', RunRecord.finished_at.is_not(None))
        .order_by(RunRecord.id.desc()).limit(RECENT_RUN_LIMIT)
        .execution_options(n2d_query_name='attention.recent_collection')
    )
    result = dict(run_id=None, source_counts=None, last_successful_read='', recent_run_limit=RECENT_RUN_LIMIT)
    for run_id, encoded in rows:
        stats = loads(encoded, {})
        if not isinstance(stats, dict):
            continue
        report = stats.get('newsnow')
        if not report and stats.get('provider') != 'newsnow':
            continue
        sources = report.get('sources') if isinstance(report, dict) else None
        counts = None
        if isinstance(sources, list) and sources:
            counts = dict.fromkeys(SOURCE_STATES, 0)
            for row in sources:
                if not isinstance(row, dict):
                    continue
                state = row.get('status')
                if not isinstance(state, str) or state not in counts:
                    continue
                counts[state] += 1
                stamp = row.get('observed_at')
                if state == 'success' and isinstance(stamp, str):
                    result['last_successful_read'] = max(result['last_successful_read'], stamp)
        if result['run_id'] is None:
            result.update(run_id=run_id, source_counts=counts)
    return result


class ReviewMatch(BaseModel):
    model_config=ConfigDict(extra='forbid')
    status:str=Field(max_length=20)


def page_context(s,**values):
    from ..collect.providers.newsnow import SOURCE_NAMES
    from ..desk.models import Watch
    from ..desk.focus_lifecycle import visible_watch
    filters=task_call(service.checked,**{k:values[k] for k in ('q','period','order','watch')})
    data=task_call(service.list_topics,s,filters,offset=values.get('offset',0),limit=values.get('limit',30))
    return dict(data=data,filters=filters,offset=values.get('offset',0),limit=values.get('limit',30),
        watches=s.exec(select(Watch.key,Watch.name).where(visible_watch()).order_by(Watch.name).limit(200)).all(),
        attention_time=display_time,source_names=SOURCE_NAMES,open_topic=values.get('attention_topic',''))


def register(app,engine):
    def db():
        with Session(engine) as s: yield s

    @app.get('/api/attention/topics')
    def topics(q:str=Query('',max_length=200),period:str='72h',order:str='attention',watch:str='',
        offset:int=Query(0,ge=0),limit:int=Query(30,ge=1,le=100),s=Depends(db)):
        filters=task_call(service.checked,q=q,period=period,order=order,watch=watch)
        return task_call(service.list_topics,s,filters,offset,limit)

    @app.get('/api/attention/topics/{key}')
    def topic(key:str,s=Depends(db)): return task_call(service.detail,s,key)

    @app.post('/api/attention/topics/{key}/select',dependencies=[Depends(same_origin)])
    def select_topic(key:str,payload:SelectCandidate):
        return task_call(service.select_topic,engine,key,**payload.model_dump())

    @app.post('/api/attention/matches/{key}',dependencies=[Depends(same_origin)])
    def review_match(key:str,payload:ReviewMatch,s=Depends(db)):
        return task_call(service.set_match,s,key,payload.status)

    @app.get('/api/attention/status')
    def status(s=Depends(db)):
        data = task_call(service.status, s, app.state.attention)
        return dict(data, collection=recent_collection(s))

    @app.post('/api/attention/rescan',dependencies=[Depends(same_origin)])
    def rescan():
        from ..attention.runtime import rescan
        task_call(rescan,engine)
        app.state.attention._backfill_at=0
        return dict(ok=True)
