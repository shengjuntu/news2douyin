"""Real PostgreSQL expiry sorting, including a stopped higher-coverage source."""
import json

from sqlalchemy import event
from sqlmodel import Session

from news2douyin.attention.service import checked, detail, list_topics
from news2douyin.storage.db import init_db, make_engine
from sql_readonly_audit import is_read_only_execution
from test_attention_freshness import topic
from test_postgres_integration import postgres_url


def test_postgres_stopped_source_is_unknown_and_cannot_displace_fresh_topic(postgres_url):
    engine = make_engine(postgres_url); init_db(engine)
    with Session(engine) as session:
        for row in (topic('older-high-coverage', 7, 10), topic('recent-one-platform', 1, 1)):
            row.chart_json = json.dumps(json.loads(row.summary_json)['series'])
            session.add(row)
        session.commit()
    statements = []
    def audit(_connection, _cursor, sql, _parameters, context, _many):
        statements.append((sql, is_read_only_execution(sql, context)))
    event.listen(engine, 'before_cursor_execute', audit)
    try:
        with Session(engine) as session:
            assert [row['key'] for row in list_topics(session, checked(period='all'))['items']] == ['recent-one-platform', 'older-high-coverage']
            assert [row['key'] for row in list_topics(session, checked(period='all', order='rising'))['items']] == ['recent-one-platform']
            old = detail(session, 'older-high-coverage')
            assert old['topic']['attention_score'] is None and old['topic']['rise_score'] is None
            assert old['series'][0]['attention_score'] == 80
        assert statements and all(read for _sql, read in statements)
        assert not any('count(' in sql.lower().replace(' ', '') for sql, _read in statements)
    finally:
        event.remove(engine, 'before_cursor_execute', audit)
        engine.dispose()
