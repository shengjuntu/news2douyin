import json
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as db_event
from sqlmodel import Session, select

from news2douyin.digest.models import DailyDigestIssue
from news2douyin.digest.service import default_day
from news2douyin.server.app import create_app
from news2douyin.topics.models import TopicSource
from news2douyin.workbench.models import ContentTask


@pytest.fixture
def daily_app(tmp_path, monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL', '')
    app = create_app(db_url='sqlite:///'+str(tmp_path/'daily.db'), storage_root=str(tmp_path/'runs'))
    day = default_day().isoformat()
    title = '许可范围更新 <script>alert(1)</script>'
    card = dict(key='progress_01',title=title,excerpt='公告列出适用范围。',
                source_kind='gkg',source_key='gkg:gkg_english:20261101000000:1',
                source_url='https://port.example/report',country='CN',channel='',watches=['港口'],
                observed_at=day+'T04:30:00Z',reasons=['专题相关：港口'],sources=[
                    dict(title=title,url='https://port.example/report',source_kind='gkg',source_key='gkg:gkg_english:20261101000000:1'),
                    dict(title='合并报道',url='https://another.example/announcements',source_kind='gdelt',source_key='gdelt:777'),
                ])
    with Session(app.state.engine) as s:
        s.add(DailyDigestIssue(day=day,status='ready',items_json=json.dumps([card]),
                               stats_json=json.dumps(dict(scanned=4,groups=1,selected=1,sources=dict(gkg_seen=250,gkg_invalid=2)))))
        s.commit()
    yield app, TestClient(app), day, card
    app.state.engine.dispose()


def test_published_edition_is_the_read_only_landing(daily_app):
    app, client, day, card = daily_app
    sqls = []
    def capture(_conn,_cursor,statement,*rest):
        sqls.append(statement)
    db_event.listen(app.state.engine, 'before_cursor_execute', capture)
    try:
        result = client.get('/radar')
        api = client.get('/api/radar/daily', params={'day':day})
    finally:
        db_event.remove(app.state.engine,'before_cursor_execute',capture)
    assert '本期最多 100 条' in result.text
    assert result.status_code == api.status_code == 200
    assert api.json()['items'] == [card]
    assert all('dailydigestissue' in stmt.lower() and stmt.lstrip().upper().startswith('SELECT') for stmt in sqls)
    html = result.text
    for text in ['进展', '焦点相关：港口', '扫描 GKG 归档行', '250', '中国', '12:30', '/api/intake/records/gkg_english/20261101000000/1', 'https://port.example/report',
                 'https://another.example/announcements', '/radar?source=gdelt', '/radar?source=local',
                 '/radar?source=attention', '形成选题', 'progress=progress_01']:
        assert text in html
    assert html.count('<summary>为什么值得跟</summary>') == 1
    for label in ('本期新增', '来源证据', '焦点关联'):
        assert f'<dt>{label}</dt>' in html
    assert f'href="/api/radar/daily/{day}/shortlist.md?revision=1">每日精选 · Markdown（1 条）' in html
    assert '/brief.md?' not in html
    assert 'cytoscape.min.js' not in html and 'd3.min.js' not in html
    assert card['title'] not in html and '&lt;script&gt;' in html
    assert 'data-radar data-facet-state=' in client.get('/radar?view=network').text


def test_archive_validation_and_pending_are_distinct_from_an_empty_edition(daily_app):
    app, client, _, _ = daily_app
    old = (default_day()-timedelta(days=15)).isoformat()
    params = dict(day=old)
    missing = client.get('/radar',params=params).text
    assert '这一天还没有生成日报' in missing and '/shortlist.md?' not in missing
    assert client.get('/api/radar/daily',params=params).json()['status']=='pending'
    with Session(app.state.engine) as s:
        s.add(DailyDigestIssue(day=old,status='ready'));s.commit()
    empty = client.get('/radar',params=params).text
    assert '本期没有入选进展' in empty and '每日精选 · Markdown（0 条）' in empty
    for raw_day in ['2026-02-30','yesterday',(default_day()+timedelta(days=1)).isoformat()]:
        for path in ['/radar','/api/radar/daily']:
            assert client.get(path,params={'day':raw_day}).status_code==422


def test_select_frozen_progress_creates_one_workspace_with_original_evidence(daily_app):
    app, client, day, card = daily_app
    fields = dict(digest_day=day,progress=card['key'])
    assert client.get('/workbench/new',params=fields).status_code == 200
    payload = dict(fields,request_id='daily-ui-select-1',title='核对许可变化',angle='哪些行业适用')
    assert client.post('/api/workbench',json=payload,headers={'Origin':'https://evil.example'}).status_code==403
    assert client.post('/api/workbench',json=dict(payload,source_url='https://invented.example')).status_code==422
    assert client.post('/api/workbench',json=dict(payload,progress='progress_missing')).status_code==404
    first = client.post('/api/workbench',json=payload)
    assert first.status_code==201,first.text
    key = first.json()['key']
    assert client.get('/workbench/'+key+'/selection').status_code == 200
    assert client.post('/api/workbench', json=payload).json()['key'] == key
    assert client.post('/api/workbench', json=dict(payload,title='不同的角度')).status_code == 409
    with Session(app.state.engine) as s:
        rows = s.exec(select(TopicSource).where(TopicSource.workspace_id==key)).all()
        docs = {doc['url']:doc for row in rows for doc in [json.loads(row.document_json)]}
        assert set(docs)=={'https://port.example/report','https://another.example/announcements'}
        doc=docs['https://port.example/report']
        assert doc['title']==card['title'] and doc['content']==card['excerpt']
        assert doc['requires_original_text'] and doc['source_kind']=='search_snippet'
        assert doc['source_ref']==dict(feed='gkg_english',stamp='20261101000000',row_index=1)
        assert len(s.exec(select(ContentTask)).all()) == 1


def test_archive_request_is_explicit_idempotent_and_worker_owned(daily_app, monkeypatch):
    app, client, _, _ = daily_app
    old=(default_day()-timedelta(days=25)).isoformat()
    monkeypatch.setenv('NEWS2DOUYIN_DIGEST_ENABLED','1')
    url='/api/radar/daily?day='+old
    def forbidden(*args,**kwargs):
        raise AssertionError('HTTP thread built the archive')
    from news2douyin.digest import service
    monkeypatch.setattr(service,'build_issue',forbidden)
    assert 'data-daily-request>' in client.get('/radar?day='+old).text
    assert client.get(url).json()['requested'] is False
    assert client.post(url, headers={'Origin':'https://evil.example'}).status_code==403
    assert client.get(url).json()['requested'] is False
    first=client.post(url)
    assert first.status_code==202 and first.json()['requested'] is True
    assert client.post(url).json()==first.json()
    assert 'data-requested="1"' in client.get('/radar?day='+old).text
    assert 'data-daily-request>' not in client.get('/radar?day='+old).text
    monkeypatch.setenv('NEWS2DOUYIN_DIGEST_ENABLED','0')
    assert client.post('/api/radar/daily?day=2024-01-01').status_code==503
    assert client.get('/api/radar/daily?day=2024-01-01').json()['requested'] is False


def test_minimum_calendar_date_can_publish_via_worker(daily_app, monkeypatch):
    app, client, _, _ = daily_app
    monkeypatch.setenv('NEWS2DOUYIN_DIGEST_ENABLED', '1')
    url = '/api/radar/daily?day=0001-01-01'
    assert client.post(url).json()['requested']
    # Just after midnight the scheduler may first produce its last eligible day.
    for _ in range(2):
        assert app.state.digest_worker.step()
        if client.get(url).json()['status'] == 'ready':
            break
    stored = client.get(url).json()
    assert stored['status'] == 'ready' and stored['items'] == []
    assert client.get('/radar?day=0001-01-01').status_code == 200


def test_daily_card_distinguishes_growth_action_and_material_provenance(daily_app):
    app, client, day, card = daily_app
    url = card['source_url']
    action = dict(kind='dated_action_report', basis='摘录同时出现日期与行动', quote='本期日期及签署行动。', url=url)
    coverage = dict(new_documents=5, new_domains=2, history_complete=True)
    cards = [
        dict(card, key='merged', headline_variants=2, action_evidence=action, official_coverage=coverage),
        dict(card, key='growth', coverage_growth_only=True, official_coverage=coverage),
        dict(card, key='recovered', reconstruction={'title_origin': 'reconstructed_sentence'}),
        dict(card, key='augmented', reconstruction={'title_origin': 'publisher'}),
        dict(card, key='weak', action_evidence=dict(action, url='https://other.example/story')),
        dict(card, key='newsnow', source_kind='newsnow', source_key='newsnow:one'),
    ]
    with Session(app.state.engine) as session:
        issue = session.get(DailyDigestIssue, day)
        issue.items_json = json.dumps(cards)
        session.commit()

    html = client.get('/radar', params={'day': day}).text

    def entry(key):
        return html.split(f'data-daily-progress="{key}">', 1)[1].split('</article>', 1)[0]

    report = entry('merged')
    assert '报道提到本期日期和行动' in report and action['quote'] in report
    assert '合并了 2 种标题写法' in report and '保存 2 个可查来源链接' in report
    assert '<h2><a href="https://port.example/report"' in report
    growth = entry('growth')
    assert '报道增加 · 行动进展待核' in growth and '本期首次关联 5 篇报道' in growth
    assert '首次出现 2 个媒体域名' in growth and '多个域名可能转载同一稿件' in growth
    assert action['quote'] not in growth
    assert 'daily-source-caption">重建片段' in entry('recovered')
    assert 'daily-source-caption">来源标题' in entry('augmented')
    assert '片段重建 · 待核对原文' in entry('augmented')
    assert action['quote'] not in entry('weak') and '本期收录' in entry('weak')
    assert '平台条目标题' in entry('newsnow') and '平台关注线索' in entry('newsnow')
    assert client.get('/api/radar/daily', params={'day': day}).json()['items'] == cards


def test_title_only_legacy_archive_keeps_working_links_for_other_cards(daily_app):
    app, client, _, card = daily_app
    day = (default_day() - timedelta(days=35)).isoformat()
    legacy = {'key': 'legacy-title', 'title': '档案中只留下了这条标题'}
    secondary = dict(legacy, key='legacy-sample', sources=[card['sources'][0]])
    stored = [legacy, secondary, card]
    with Session(app.state.engine) as session:
        session.add(DailyDigestIssue(day=day, status='ready', items_json=json.dumps(stored)))
        session.commit()
    archive = client.get('/radar', params={'day': day})
    assert archive.status_code == 200
    old, sample, full = archive.text.split('<article class="daily-entry"')[1:]
    assert legacy['title'] in old and '归档标题' in old and '<h2><a ' not in old
    assert '打开关联报道' not in old and 'https://' not in old
    assert old.count('<summary>为什么值得跟</summary>') == 1 and '形成选题' not in old
    assert '<ul class="daily-source-list">' not in old
    assert '保存 1 个可查来源链接' in sample and 'progress=legacy-sample' in sample
    assert 'progress=progress_01' in full and '形成选题' in full
    assert client.get('/api/radar/daily', params={'day': day}).json()['items'] == stored
    params = dict(digest_day=day, progress='legacy-title')
    assert client.get('/workbench/new', params=params).status_code == 422
    assert client.post('/api/workbench', json=dict(params, request_id='legacy-no-source')).status_code == 422
    created = client.post('/api/workbench', json=dict(digest_day=day, progress='legacy-sample', request_id='legacy-sample'))
    assert created.status_code == 201
    with Session(app.state.engine) as session:
        sources = session.exec(select(TopicSource).where(TopicSource.workspace_id == created.json()['key'])).all()
        assert [json.loads(item.document_json)['url'] for item in sources] == [card['source_url']]


def test_publisher_breadth_uses_only_frozen_counts_and_links(daily_app):
    app, client, day, card = daily_app
    breadth = dict(units=2, units_capped=False, mapped_families=['iHeartMedia'],
                   unmatched_hosts=1, unmatched_hosts_capped=False, mapping_version='historical-map')
    cards = [
        dict(card, key='folded', domain_count=6, publisher_breadth=breadth),
        dict(card, key='unmapped', domain_count=4, publisher_breadth=dict(
            breadth, units=4, mapped_families=[], unmatched_hosts=4)),
        dict(card, key='capped', domain_count=64, domains_capped=True, publisher_breadth=dict(
            breadth, units=64, units_capped=True, unmatched_hosts=64, unmatched_hosts_capped=True)),
        dict(card, key='historic', domain_count=5, domains=['a.iheart.com','b.iheart.com']),
        dict(card, key='partial', domain_count=5, publisher_breadth={'mapped_families':['iHeartMedia']}),
    ]
    with Session(app.state.engine) as session:
        session.get(DailyDigestIssue, day).items_json = json.dumps(cards)
        session.commit()

    response = client.get('/radar', params={'day':day})
    assert response.status_code == 200
    folded, unmapped, capped, historic, partial = response.text.split('<article class="daily-entry"')[1:]
    top, disclosure = folded.split('<details class="daily-evidence">')
    assert 'iHeartMedia' not in top
    assert '6 个报道域名' in disclosure and '2 个排序来源组' in disclosure
    assert 'iHeartMedia' in disclosure and '1 个域名的媒体归属待核' in disclosure
    assert '本期归属表 historical-map' in disclosure
    assert '>another.example · GDELT Events</span>' in disclosure and card['source_url'] in top
    count_details = unmapped.split('<details class="daily-evidence">')[1]
    assert '4 个报道域名' in count_details and '4 个排序来源组' in count_details
    assert '4 个域名的媒体归属待核' in unmapped
    assert '至少 64 个报道域名' in capped and '至少 64 个排序来源组' in capped
    assert '至少 64 个域名的媒体归属待核' in capped
    assert 'data-publisher-breadth' not in historic and 'data-publisher-breadth' not in partial
    assert client.get('/api/radar/daily', params={'day':day}).json()['items'] == cards


def test_grouped_reading_keeps_each_item_and_numbers_visible_order(daily_app):
    app, client, day, base = daily_app
    multi = dict(countries=['CA', 'MX'], primary='', basis='gkg_mentions')
    ca = dict(countries=['CA','FR'], primary='CA', basis='gkg_mentions')
    cards = [
        dict(base, key='unknown', country='', publisher_country='US', observed_at='missing'),
        dict(base, key='early', country='', geography=ca, observed_at=day+'T14:00:00+08:00',
             category={'label':'经济与贸易'}),
        dict(base, key='multi', country='', geography=multi, channel='外交', observed_at=''),
        dict(base, key='late', country='CA', observed_at=day+'T08:00:00Z'),
    ]
    with Session(app.state.engine) as s:
        s.get(DailyDigestIssue, day).items_json = json.dumps(cards)
        s.commit()

    def read(**params):
        response = client.get('/radar', params=params)
        assert response.status_code == 200
        return response.text

    def keys(html):
        return re.findall(r'data-daily-progress="([^"]+)"', html)

    assert keys(read()) == ['unknown', 'early', 'multi', 'late']
    geo = read(group_by='country', sort_by='recent')
    assert keys(geo) == ['late', 'early', 'multi', 'unknown']
    assert re.findall(r'data-reading-ordinal="(\d+)"', geo) == ['1', '2', '3', '4']
    for label in ['加拿大（2）', '多国或多地区（1）', '国家或地区未标注（1）', '报道提及 · 加拿大、法国']:
        assert label in geo
    assert '美国' not in geo
    assert 'group_by=country&amp;sort_by=recent' in geo
    assert '经济与贸易（1）' in read(group_by='category')
    assert '未分类（2）' in read(group_by='category')
    assert 'GDELT GKG（4）' in read(group_by='source')
    assert 'data-group-jump' not in read()
    assert client.get('/api/radar/daily', params={'day':day}).json()['items'] == cards
    assert client.get('/radar?group_by=publisher_country').status_code == 422
    assert client.get('/radar?sort_by=unknown').status_code == 422


def test_refresh_retains_readable_edition_and_poll_only_loads_state(daily_app):
    app, client, day, card = daily_app
    published = dict(revision=4, kind='intraday', cutoff_at=day+'T04:00:00Z')
    sources = dict(backfill_id='archive-1', gkg_restored_archives=1, gkg_restored_rows=12,
                   gkg_restored_batches={'gkg_english:20261101000000':day+'T05:00:00Z'})
    with Session(app.state.engine) as s:
        row = s.get(DailyDigestIssue, day)
        row.stats_json = json.dumps(dict(publication=published, sources=sources))
        s.commit()
    today = client.get('/radar').text
    assert f'max="{day}"' in today and '重新生成今日精选' in today and '今日滚动版 · 第 4 版' in today
    assert '资料截至' in today and '12:00' in today and '历史补录 · 实际入库' in today and '13:00' in today
    assert f'/admin/daily-radar?day={day}' in today
    queued = client.post('/api/radar/daily', params={'day':day})
    assert queued.status_code == 202 and queued.json()['build_status'] == 'pending'
    pending = client.get('/radar').text
    assert '当前显示上一版' in pending and f'data-daily-progress="{card["key"]}"' in pending and 'data-poll="1"' in pending
    assert f'href="/api/radar/daily/{day}/shortlist.md?revision=4"' in pending
    assert 'data-daily-request>' not in pending

    sql = []
    def trace(conn, cursor, query, args, context, many):
        sql.append(query.lower())
    db_event.listen(app.state.engine, 'before_cursor_execute', trace)
    try:
        state = client.get('/api/radar/daily', params={'day':day,'summary':'true'}).json()
    finally:
        db_event.remove(app.state.engine, 'before_cursor_execute', trace)
    assert state['revision'] == 4 and 'items' not in state and len(sql) == 1
    assert 'items_json' not in sql[0] and 'keys_json' not in sql[0]
    with Session(app.state.engine) as s:
        row = s.get(DailyDigestIssue, day)
        row.status, row.requested, row.error = 'refresh_failed', False, '读取失败'
        s.commit()
    failed = client.get('/radar').text
    assert '仍显示上一版' in failed and '重试今日更新' in failed and 'data-poll="0"' in failed
    assert f'data-daily-progress="{card["key"]}"' in failed


def test_daily_disclosure_keeps_event_and_observation_dates_distinct(daily_app):
    app, client, day, base = daily_app
    card = dict(base, key='late-observation', source_kind='gdelt', source_key='gdelt:7891',
                source_clock={'event_day': '2025-03-15', 'clock_kind': 'official',
                              'upstream_first': '2025-03-16T03:20:00Z',
                              'upstream_last': day+'T02:45:00Z',
                              'local_received_at': day+'T04:07:00Z'})
    with Session(app.state.engine) as session:
        row = session.get(DailyDigestIssue, day)
        row.items_json = json.dumps([card])
        session.commit()
    response = client.get('/radar', params={'day': day})
    assert response.status_code == 200
    detail = response.text.split('<summary>为什么值得跟</summary>')[1].split('</details>', 1)[0]
    assert '2025-03-15' in detail and '11:20' in detail and '12:07' in detail
    assert '港口' in detail and '/focus/' not in detail


def test_focus_clause_links_the_matching_story_and_recorded_question(daily_app):
    app, client, day, base = daily_app
    match = dict(kind='membership', watch_key='w_trade', url='https://another.example/announcements',
                 question='许可变更是否影响供应商？')
    card = dict(base, focus_evidence={'港口': match}, focus_hints=['关税'])
    with Session(app.state.engine) as session:
        session.get(DailyDigestIssue, day).items_json = json.dumps([card])
        session.commit()
    html = client.get('/radar', params={'day': day}).text
    focus = html.split('<dt>焦点关联</dt><dd>')[1].split('</dd>')[0]
    assert 'href="/focus/w_trade"' in focus and match['question'] in focus
    assert match['url'] in focus and '关联报道' in focus
    assert '关税' in focus and '待核' in focus

def test_budget_and_strategy_come_from_frozen_edition(daily_app):
    app, client, day, _ = daily_app
    with Session(app.state.engine) as session:
        row = session.get(DailyDigestIssue, day)
        stats = json.loads(row.stats_json)
        stats.update(scanned=380, strategy_consumed=117, selection=dict(strategy='regional_signal', version='1.2', limit=200, options={}))
        row.stats_json = json.dumps(stats)
        session.commit()
    response = client.get('/radar', params={'day': day})
    assert response.status_code == 200
    assert '本期最多 200 条' in response.text and 'regional_signal（1.2）' in response.text
    assert '<dt>来源提供的候选记录</dt><dd>380</dd>' in response.text
    assert '<dt>策略实际读取的候选记录</dt><dd>117</dd>' in response.text
