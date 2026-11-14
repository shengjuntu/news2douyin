"""Topic graph progress survives additive upgrades and SQLite→PostgreSQL copy."""
from sqlalchemy import text
from sqlmodel import Session

from news2douyin.desk.graph_focus_models import GraphFocusReceipt, GraphFocusState
from news2douyin.desk.models import DeskMeta, GraphPending
from news2douyin.storage.db import create_schema, init_db, make_engine
from news2douyin.storage.migrate import migrate_sqlite
from test_postgres_migration import target


def test_additive_topic_tables_preserve_existing_queue_and_owner(tmp_path):
    engine = make_engine('sqlite:///'+str(tmp_path/'upgrade.db'))
    init_db(engine)
    with Session(engine) as session:
        session.add(GraphPending(key='watch/existing', token='new', written='old'))
        session.add(DeskMeta(key='graph_owner', value='existing-owner'))
        session.commit()
    with engine.begin() as connection:
        connection.execute(text('DROP TABLE graphfocusreceipt'))
        connection.execute(text('DROP TABLE graphfocusstate'))
    create_schema(engine)
    with Session(engine) as session:
        row = session.get(GraphPending, 'watch/existing')
        assert (row.token, row.written) == ('new', 'old')
        assert session.get(DeskMeta, 'graph_owner').value == 'existing-owner'
    with engine.connect() as connection:
        assert connection.execute(text('SELECT count(*) FROM graphfocusreceipt')).scalar() == 0
        assert connection.execute(text('SELECT count(*) FROM graphfocusstate')).scalar() == 0
    engine.dispose()


def test_sqlite_to_postgres_preserves_topic_cursor_receipts_and_pending_token(tmp_path, target):
    path = tmp_path/'focus-checkpoints.db'
    source = make_engine('sqlite:///'+str(path))
    init_db(source)
    with Session(source) as session:
        session.add(GraphFocusState(namespace='original_namespace', watch='专题 Ω', token='captured-pass',
            scope_version=7, rebuild_epoch='schema-rebuild-3', generation='active-pass', phase='events', cursor='é_visited',
            through_key='中国_upper', adopted=True, adopt_cursor=2**40, adopt_through=2**40+4096,
            scanned=2**40, changed=17, removed=3, batches=99))
        session.add(GraphFocusReceipt(namespace='original_namespace', watch='专题 Ω', kind='events',
            item='é_visited', locator='{}', fingerprint='stable-content-hash', generation='active-pass'))
        session.add(GraphFocusReceipt(namespace='original_namespace', watch='专题 Ω', kind='point_material',
            item='point-material-a', locator=' {"point":"问题 Ω", "event":"é_visited"} ',
            fingerprint='prior-material-hash', generation='previous-pass'))
        session.add(GraphPending(key='watch/专题 Ω', token='newer-live-update', written='last-complete'))
        session.add(DeskMeta(key='graph_owner', value='unchanged-owner'))
        session.commit()
    source.dispose()
    report = migrate_sqlite(path, target, source_stopped=True)
    tables = {row['table']: row for row in report['tables']}
    for name, count in [('graphfocusstate', 1), ('graphfocusreceipt', 2)]:
        assert tables[name]['source_rows'] == tables[name]['target_rows'] == count
        assert tables[name]['source_sha256'] == tables[name]['target_sha256']
    destination = make_engine(target)
    with Session(destination) as session:
        state = session.get(GraphFocusState, ('original_namespace', '专题 Ω'))
        assert (state.phase, state.cursor, state.through_key) == ('events', 'é_visited', '中国_upper')
        assert (state.token, state.scope_version, state.generation, state.adopted) == ('captured-pass', 7, 'active-pass', True)
        assert (state.scanned, state.changed, state.removed, state.batches) == (2**40, 17, 3, 99)
        assert (state.rebuild_epoch, state.adopt_cursor, state.adopt_through) == ('schema-rebuild-3', 2**40, 2**40+4096)
        receipt = session.get(GraphFocusReceipt, ('original_namespace', '专题 Ω', 'events', 'é_visited'))
        assert (receipt.fingerprint, receipt.generation) == ('stable-content-hash', 'active-pass')
        locator = session.get(GraphFocusReceipt, ('original_namespace', '专题 Ω', 'point_material', 'point-material-a'))
        assert locator.locator == ' {"point":"问题 Ω", "event":"é_visited"} '
        assert locator.generation == 'previous-pass'
        queue = session.get(GraphPending, 'watch/专题 Ω')
        assert (queue.token, queue.written) == ('newer-live-update', 'last-complete')
        assert session.get(DeskMeta, 'graph_owner').value == 'unchanged-owner'
    destination.dispose()
