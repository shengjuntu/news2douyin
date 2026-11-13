"""New graph maintenance remains resumable through the supported DB migration."""
from sqlmodel import Session

from news2douyin.desk.graph_maintenance_models import GraphGcState
from news2douyin.desk.models import DeskMeta,GraphPending
from news2douyin.storage.db import init_db,make_engine
from news2douyin.storage.migrate import migrate_sqlite
from test_postgres_migration import target


def test_sqlite_to_postgres_keeps_gc_cursor_ack_and_watermark(tmp_path,target):
    path=tmp_path/'maintenance.db'
    source=make_engine('sqlite:///'+str(path));init_db(source)
    with Session(source) as s:
        s.add(GraphGcState(key='gc-preserved',namespace='original_namespace',graph='active_global',
            label='Article',cursor=2**40,through_key=2**40+1024,cycle=7,status='sweeping',
            phase='batch',scanned=2**40,deleted=123,next_due=0))
        s.add(GraphPending(key='event/old',token='new-pending-token',written='last-complete-token'))
        s.add(DeskMeta(key='graph_projection_epoch_1',value='stable-completed-epoch'))
        s.add(DeskMeta(key='graph_owner',value='preserved-owner'))
        s.commit()
    source.dispose()
    report=migrate_sqlite(path,target,source_stopped=True)
    gc=next(row for row in report['tables'] if row['table']=='graphgcstate')
    assert gc['source_rows']==gc['target_rows']==1 and gc['source_sha256']==gc['target_sha256']
    destination=make_engine(target)
    with Session(destination) as s:
        row=s.get(GraphGcState,'gc-preserved')
        assert row.cursor==2**40 and row.scanned==2**40 and row.cycle==7 and row.phase=='batch'
        assert s.get(GraphPending,'event/old').written=='last-complete-token'
        assert s.get(DeskMeta,'graph_projection_epoch_1').value=='stable-completed-epoch'
        assert s.get(DeskMeta,'graph_owner').value=='preserved-owner'
    destination.dispose()
