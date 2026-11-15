"""Source previews use the recent-event index before ranking confidence."""
from datetime import datetime,timedelta,timezone

from sqlalchemy import event as sql_event
from sqlmodel import Session

from news2douyin.intake.official_models import OfficialMention
from news2douyin.selection.event_content import mention_sources,RECENT_MENTION_CANDIDATES
from news2douyin.storage.db import make_engine


def test_old_high_confidence_history_is_outside_the_bounded_preview_candidate_set(tmp_path):
    engine=make_engine('sqlite:///'+str(tmp_path/'candidate-plan.db'))
    OfficialMention.__table__.create(engine)
    latest=datetime(2026,11,14,tzinfo=timezone.utc)
    def record(n,recent):
        key=('recent-' if recent else 'old-')+str(n)
        at=latest-timedelta(seconds=n if recent else 1000+n)
        return dict(key=key,event_id='1001',doc_identifier='https://report.example/'+key,
            last_seen=at.isoformat(),confidence=n if recent else 100,
            first_feed='mentions_english',first_stamp='20261114000000',first_row_index=n,
            last_feed='mentions_english',last_stamp='20261114000000',last_row_index=n)
    try:
        with engine.begin() as c:
            c.execute(OfficialMention.__table__.insert(),[record(n,False) for n in range(1000)]
                +[record(n,True) for n in range(RECENT_MENTION_CANDIDATES)])
        executions=[]
        def audit(_conn,_cursor,statement,parameters,*_):executions.append((statement,parameters))
        sql_event.listen(engine,'before_cursor_execute',audit)
        try:
            with Session(engine) as session:items=mention_sources(session,['1001'])['1001']
        finally:sql_event.remove(engine,'before_cursor_execute',audit)
        assert [row['key'] for row in items]==['recent-11','recent-10','recent-9']
        assert len(executions)==1
        statement,parameters=executions[0]
        assert 'row_number' not in statement.lower()
        with engine.connect() as c:
            candidates=c.exec_driver_sql(statement,parameters).all()
            plan=c.exec_driver_sql('EXPLAIN QUERY PLAN '+statement,parameters).all()
        assert len(candidates)==RECENT_MENTION_CANDIDATES
        details=' '.join(str(row[-1]) for row in plan)
        assert 'ix_officialmention_event_recent' in details,details
        assert 'TEMP B-TREE' not in details,details
    finally:engine.dispose()
