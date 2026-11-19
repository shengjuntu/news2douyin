from dataclasses import replace
from datetime import date
import json

from sqlmodel import Session

from news2douyin.desk.focus_models import FocusBackfill, FocusDefinition, FocusTrash
from news2douyin.desk.models import Watch
from news2douyin.digest.focus_hints import MAX_HINTS, annotate_focus, read_focus_hints
from news2douyin.digest.ranking import Candidate


DAY = date(2026, 11, 1)
OLD = '2026-10-31T00:00:00Z'
END = '2026-11-01T16:00:00Z'


def watch(name, scope, **fields):
    return Watch(key=name, name=name, kind='长期事件', scope=scope,
                 **dict({'created': OLD}, **fields))


def test_wire_headline_requires_both_dimensions_and_anchors_honours_aliases(engine):
    with Session(engine) as s:
        s.add_all([watch('法国劳工', '法国', dimensions='罢工', exclude='足球, football'),
                   watch('人物', '特朗普'),
                   watch('自定义', '沪港', dimensions='停航'),
                   FocusDefinition(watch='自定义', description='track', updated=OLD,
                                   spec_json=json.dumps({'entities': [{'name': '沪港', 'aliases': ['Bay Link']}]})),
                   watch('空', '，；')])
        s.commit()
        match = read_focus_hints(s, DAY)
    assert match('PARIS workers on strike') == ('法国劳工',)
    assert match('France hosts a market conference') == ()
    assert match('French workers on strike at football stadium') == ()
    assert match('Bay Link发布停航公告') == ('自定义',)
    assert match('在ＴＲＵＭＰ会见前') == ('人物',)
    assert match('A trumpeter tours France') == ()


def test_exclusive_cutoff_visibility_overlaps_and_cap(engine):
    with Session(engine) as s:
        s.add_all([watch('1 inactive', '亚太', enabled=False),
                   watch('2 deleted', '亚太'),
                   FocusTrash(watch='2 deleted', name='2 deleted', deleted_at=OLD),
                   watch('3 late', '亚太', created=END),
                   watch('4 unknown', '亚太', version=2),
                   watch('5 edited later', '亚太', version=2),
                   FocusBackfill(watch='5 edited later', scope_version=2, updated=END),
                   watch('6 ready', '亚太地区', version=2),
                   FocusBackfill(watch='6 ready', scope_version=2, updated=OLD)])
        s.add_all([watch(f'区域{i:03}', '亚太') for i in range(100)])
        s.commit()
        match = read_focus_hints(s, DAY)
    names = match('亚太地区本周动态')
    assert len(names) == MAX_HINTS and names[0] == '6 ready'
    assert names[1:] == tuple(f'区域{i:03}' for i in range(MAX_HINTS-1))


def test_annotations_preserve_local_decisions_and_counts(engine):
    raw = Candidate('gkg:row', 'Trump issues update', OLD, 'https://news.example/a', 'gkg')
    with Session(engine) as s:
        empty = read_focus_hints(s, DAY)
        assert not empty and list(annotate_focus([raw], empty)) == [raw]
        s.add(watch('人物', '特朗普'))
        s.commit()
        match = read_focus_hints(s, DAY)
    local = replace(raw, source_kind='local')
    accepted = replace(raw, watches=('专家专题',))
    counts = {}
    hints = list(annotate_focus([raw, local, accepted], match, stats=counts))
    assert hints[0].focus_hints == ('人物',) and hints[0].watches == ()
    assert hints[1] is local and hints[2] is accepted
    assert counts == {'literal_focus_hints': 1}
