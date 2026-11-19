"""The handoff to Codex is a lossless list of seats, independent of video selection."""
import re

import pytest
from sqlalchemy import event
from sqlmodel import Session

from news2douyin.digest.brief import render_brief
from news2douyin.digest.brief_export import markdown_for_shortlist
from news2douyin.digest.shortlist import render_shortlist
from news2douyin.translations.daily import daily_card_descriptor


def issue(items, **fields):
    return dict(dict(status='ready', day='2026-11-15', revision=3, edition_kind='intraday',
                     cutoff_at='2026-11-15T07:31:52Z', items=items), **fields)


def test_whole_edition_preserves_all_seats_and_duplicate_headlines():
    cards = [dict(key=f'progress_{n}', title='Regional wire bulletin', source_kind='gkg',
                  source_url=f'https://news.example/{n}') for n in range(100, 0, -1)]
    cards[19]['title'] = 'Sponsored: where to watch the special'
    cards[51]['source_url'] = ''
    cards[80]['title'] = ''
    md = render_shortlist(issue(cards))
    sections = re.split(r'^## (\d{3})\. ', md, flags=re.M)
    assert '100 条' in sections[0] and '第 3 版' in sections[0] and '15:31:52' in sections[0]
    assert len(sections) == 201
    for seat, (number, content) in enumerate(zip(sections[1::2], sections[2::2]), 1):
        assert int(number) == seat
        assert f'`progress_{101-seat}`' in content
        if seat != 52:
            assert f'https://news.example/{101-seat}>' in content
    assert 'Sponsored: where to watch the special' in md and '本版未保存标题' in md


def test_evidence_and_full_length_text_stay_with_their_source():
    title = 'Ministers sign a treaty'
    excerpt = '甲'*890+'结束'
    url = 'https://publisher.example/photo)(?num=1&a=2'
    card = dict(key='progress_key', title=title, excerpt=excerpt, language='fr', source_kind='gdelt',
                source_url=url, source_key='gdelt:654321', observed_at='2026-11-15T01:00:00Z',
                watches=['滞后议题'], focus_hints=['贸易'], actors=['Minister A'],
                focus_evidence={'贸易':dict(kind='scope_hint',terms=['treaty'],url='https://other.example/report')},
                geography=dict(countries=['FR'],basis='official_action'), channel='谈判',
                source_clock=dict(event_day='2026-11-14',clock_kind='official',upstream_last='2026-11-15T01:00:00Z'),
                sources=[dict(url='https://other.example/report',title='A second report'),
                         dict(url='javascript:alert(1)',title='Untrusted')],
                reasons=['editor [link](evil)'],candidate_documents=12,domain_count=10,
                publisher_breadth=dict(units=7,units_capped=True))
    md = render_shortlist(issue([card]), translations={card['key']:'译文'*640+'译尾'})
    assert excerpt in md and '译尾' in md and '法语' in md
    for value in ('代表记录 `gdelt:654321`', 'GDELT Events', '行动地 法国', '同组参与方 Minister A',
                  'GDELT 编码事件日期 2026-11-14','GDELT 最近观测 2026-11-15 09:00:00',
                  '同组本地事件已纳入该专题','来源标题命中关注词','A second report',
                  '链接列表仅列出当期保存的来源样本','至少 7 个排序来源组','%29%28','&amp;a=2'):
        assert value in md
    assert 'javascript:alert' not in md


def test_old_missing_fields_text_safety_and_partial_issue():
    card = dict(title='X<script>\n## injected [pay](javascript:foo)', excerpt='one\u202etwo', source_url='')
    snapshot = issue([card], backfill_id='restore', cutoff_at='', edition_kind='closed')
    desc = daily_card_descriptor(snapshot['day'],card)
    md = render_shortlist(snapshot,translations={desc['key']:'译文\n## injection'})
    assert len(re.findall(r'^## ', md, re.M)) == 1
    assert '<script>' not in md and '(javascript:foo)' not in md and '\u202e' not in md
    assert '旧版未保存' in md and '历史补采' in md and '仅 100' not in md
    assert '日期待核' in md and '1 条' in md and '译文' in md and 'legacy_' in md
    assert '全部候选' in render_shortlist(issue([])) and '0 条' in render_shortlist(issue([]))
    with pytest.raises(ValueError):
        render_shortlist(dict(snapshot,status='building'))


def test_many_foreign_cards_need_at_most_one_read_and_no_writes(engine):
    cards=[dict(title=f'Press story {n}',source_url='https://source.example/story')
           for n in range(100)]
    operations=[]
    def track(conn, cursor, statement, *args):
        operations.append(statement.split()[0])
    with Session(engine) as session:
        event.listen(engine,'before_cursor_execute',track)
        try:
            md=markdown_for_shortlist(session,issue(cards))
        finally:
            event.remove(engine,'before_cursor_execute',track)
    assert operations==['SELECT']
    assert '## 100. Press story 99' in md

@pytest.mark.parametrize('render', [render_brief, render_shortlist])
def test_markdown_identifies_custom_selection_and_preserves_default_bytes(render):
    default = dict(strategy='balanced', version='0.37.0', options={}, limit=100)
    frozen = issue([])
    original = render(frozen)
    assert render(dict(frozen, stats={'selection': default})) == original
    for record in [dict(default, strategy='regional'), dict(default, limit=135), dict(default, options={'sample': 10})]:
        exported = render(dict(frozen, stats={'selection': record}))
        assert f"日报选取策略　{record['strategy']} · 版本 0.37.0 · 最多 {record['limit']} 条" in exported
