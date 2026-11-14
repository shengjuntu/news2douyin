"""The supported database migration preserves an operator pause and scan cursor."""
from sqlmodel import Session

from news2douyin.desk import graph_gc
from news2douyin.desk.graph import GraphMirror
from news2douyin.desk.graph_maintenance_models import GraphGcState
from news2douyin.storage.db import make_engine, init_db
from news2douyin.storage.migrate import migrate_sqlite
from test_postgres_migration import target


def test_gc_pause_and_existing_checkpoint_survive_sqlite_to_postgres(tmp_path,target):
    path=tmp_path/'pause.db'
    source=make_engine('sqlite:///'+str(path));init_db(source)
    mirror=GraphMirror();mirror.prefix='paused_namespace'
    with Session(source) as session:
        session.add(GraphGcState(key='preserved-pause-cursor',namespace=mirror.prefix,graph='active_global',
            label='Article',status='sweeping',phase='batch',cursor=321,through_key=999,cycle=7,
            scanned=123,deleted=4,cycle_scanned=10,cycle_deleted=2,next_due=0))
        session.commit()
    graph_gc.set_paused(source,mirror,True)
    source.dispose()
    result=migrate_sqlite(path,target,source_stopped=True)
    table=next(row for row in result['tables'] if row['table']=='graphgccontrol')
    assert table['source_rows']==table['target_rows']==1
    assert table['source_sha256']==table['target_sha256']
    destination=make_engine(target)
    assert graph_gc.control(destination,mirror)['paused'] is True
    with Session(destination) as session:
        state=session.get(GraphGcState,'preserved-pause-cursor')
        assert (state.cursor,state.through_key,state.cycle,state.phase)==(321,999,7,'batch')
        assert (state.scanned,state.deleted,state.cycle_scanned,state.cycle_deleted)==(123,4,10,2)
    destination.dispose()
