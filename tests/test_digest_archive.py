from datetime import date

from news2douyin.digest.archive import iter_gkg
from news2douyin.intake.lake import Lake


def test_gkg_archive_window_and_readable_evidence(tmp_path):
    day = date(2026, 11, 14)
    lake = Lake(tmp_path)
    batches = [
        ('gkg_translation', '20261111081500', '2026-11-13T16:00:00Z', [
            ('监管部门公布电池出口新规', 'https://news.example/a', True),
            ('', 'https://news.example/b', True), ('bad', 'data:text/html,bad', True),
            ('invalid', 'https://news.example/d', False)]),
        ('gkg_english', '20261114160000', '2026-11-14T16:00:00Z', [
            ('Next Beijing day', 'https://news.example/e', True)])]
    with lake.connection(create=True) as db:
        for feed, stamp, observed, rows in batches:
            db.execute("INSERT INTO intake_archives (feed,stamp,rows,completed_at) VALUES (?,?,?,?)",
                       [feed, stamp, len(rows), observed])
            for idx, (title, url, valid) in enumerate(rows, 1):
                db.execute("INSERT INTO intake_records_v2 (feed,stamp,row_index,title,url,valid) VALUES (?,?,?,?,?,?)",
                           [feed, stamp, idx, title, url, valid])
    stats = {}
    result = list(iter_gkg(tmp_path, day, stats=stats))
    assert [item.title for item in result] == ['监管部门公布电池出口新规']
    assert result[0].key == 'gkg:gkg_translation:20261111081500:1'
    assert result[0].observed_at == batches[0][2]
    assert (stats['gkg_seen'], stats['gkg_no_readable'], stats['gkg_invalid'], stats['gkg_emitted']) == (4, 2, 1, 1)
    assert list(iter_gkg(tmp_path, day, cutoff=batches[0][2])) == []


def test_empty_archive_never_created_by_read(tmp_path):
    stats = {}
    assert list(iter_gkg(tmp_path, date(2026, 11, 14), stats=stats)) == []
    assert stats['gkg_archive_present'] is False
    assert not Lake(tmp_path).path.exists()
