"""Adding old history must not make three report previews scan all Mentions.

Count SQLite VM instructions rather than wall time: this catches an unbounded
read of one hot event while remaining independent of host CPU contention.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session

from news2douyin.intake.official_models import OfficialMention
from news2douyin.selection.event_content import mention_sources
from news2douyin.storage.db import make_engine


@pytest.mark.parametrize('tied_batch_timestamps', [False, True])
def test_report_preview_read_work_is_bounded_as_an_events_old_history_grows(tmp_path, tied_batch_timestamps):
    engine = make_engine('sqlite:///'+str(tmp_path/'mention-budget.db'))
    OfficialMention.__table__.create(engine)
    latest = datetime(2026, 11, 14, tzinfo=timezone.utc)

    def add_history(start, end):
        rows = [dict(key='mention-'+str(n), event_id='1',
            doc_identifier='https://reports.example/'+str(n),
            last_seen=(latest-timedelta(seconds=0 if tied_batch_timestamps else n)).isoformat(), confidence=n % 100,
            first_feed='mentions_english', first_stamp='20261114000000', first_row_index=n,
            last_feed='mentions_english', last_stamp='20261114000000', last_row_index=n)
            for n in range(start, end)]
        with engine.begin() as connection:
            connection.execute(OfficialMention.__table__.insert(), rows)

    def read_cost():
        instructions = [0]
        def progress():
            instructions[0] += 1
            return 0
        with Session(engine) as session:
            native = session.connection().connection.driver_connection
            native.set_progress_handler(progress, 1)
            try:
                result = mention_sources(session, ['1'])
            finally:
                native.set_progress_handler(None, 0)
        assert len(result['1']) == 3
        return instructions[0]

    try:
        add_history(0, 100)
        small = read_cost()
        add_history(100, 5000)
        large = read_cost()
        assert large <= small*3, dict(mentions_before=100, mentions_after=5000,
            returned_reports=3, instructions_before=small, instructions_after=large)
    finally:
        engine.dispose()
