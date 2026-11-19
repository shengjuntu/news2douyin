from sqlmodel import Session

from news2douyin.attention.models import AttentionSelection
from news2douyin.attention.service import _selected
from news2douyin.intake.official_models import OfficialEvent
from news2douyin.selection.models import SelectionOrigin
from news2douyin.selection.service import detail as official_detail
from news2douyin.topics.models import TopicHandoff, TopicSource, TopicWorkspace

from test_research import research


def create_draft(client, title, request_id):
    response = client.post('/api/workbench', json={
        'request_id': request_id, 'title': title, 'articles': ['seed'],
    })
    assert response.status_code == 201, response.text
    return response.json()


def test_remove_draft_and_restore_with_frozen_sources(research):
    app, client, _ = research
    draft = create_draft(client, '可恢复的选题', 'trash-test-selection')
    key = draft['key']
    other = create_draft(client, '另一条选题', 'trash-test-other')
    page_path = f'/workbench/{key}/selection'
    api_path = f'/api/workbench/{key}'
    initial = client.get(page_path)
    assert initial.status_code == 200
    assert 'data-work-trash="remove"' in initial.text
    assert 'data-work-trash="remove"' in client.get('/workbench?view=selection').text
    assert client.delete(api_path, params={'version': 0}, headers={'Origin': 'https://foreign.example'}).status_code == 403

    with Session(app.state.engine) as s:
        s.add(OfficialEvent(event_id='456', event_date='2025-02-01',
            feed='events_english', stamp='20250201150000', row_index=1))
        s.add(SelectionOrigin(task=key, event_id='456', request_hash='original', snapshot_json='{}'))
        s.add(AttentionSelection(task=key, topic='source-topic', request_hash='original'))
        s.commit()
        assert official_detail(s, '456')['event']['selected_task'] == key
        assert _selected(s, ['source-topic']) == {'source-topic': key}

    assert client.post('/api/research/setup').status_code == 200
    messages_url = f'/api/topics/{key}/messages'
    message = dict(revision=1, request_id='trash-before-msg', text='比较材料')
    queued = client.post(messages_url, json=message)
    assert queued.status_code == 202, queued.text
    run = queued.json()['run_id']

    removed = client.delete(api_path, params={'version': 0})
    assert removed.status_code == 200, removed.text
    assert removed.json() == {'key': key, 'trashed': True}
    listing = client.get('/workbench?view=selection').text
    assert draft['title'] not in listing and other['title'] in listing
    assert key not in [w['workspace_id'] for w in client.get('/api/topics').json()]
    redirect = client.get(page_path, follow_redirects=False)
    assert redirect.status_code == 303 and redirect.headers['location'] == '/workbench?view=trash'
    trash = client.get('/workbench?view=trash').text
    assert draft['title'] in trash and other['title'] not in trash
    assert 'data-work-trash="restore"' in trash and 'data-work-trash-version="1"' in trash
    assert client.post(api_path + '/phase', json={'version': 1, 'phase': 'research'}).status_code == 409
    put = client.put(f'/api/topics/{key}', json={
        'revision': 1, 'title': '陈旧页面修改', 'scope': {}, 'source_ids': [],
    })
    assert put.status_code == 400 and '请先恢复' in put.text
    blocked = client.post(messages_url, json=dict(message, request_id='trash-after-msg'))
    assert blocked.status_code == 400 and '请先恢复' in blocked.text
    prior_messages = client.get(f'/api/topics/{key}').json()['messages']
    assert [(m['run_id'], m['status']) for m in prior_messages] == [(run, 'queued')]
    assert client.post('/api/workbench', json={
        'request_id': 'trash-test-selection', 'title': draft['title'], 'articles': ['seed'],
    }).status_code == 409

    source_id = draft['sources'][0]['source_id']
    with Session(app.state.engine) as s:
        assert s.get(TopicWorkspace, key).title == draft['title']
        assert s.get(TopicSource, source_id).workspace_id == key
        assert s.get(SelectionOrigin, key).event_id == '456'
        assert official_detail(s, '456')['event']['selected_task'] == ''
        assert _selected(s, ['source-topic']) == {}

    assert client.post(api_path + '/restore', params={'version': 0}).status_code == 409
    restored = client.post(api_path + '/restore', params={'version': 1})
    assert restored.status_code == 200 and restored.json()['trashed'] is False
    assert client.delete(api_path, params={'version': 0}).status_code == 409
    assert client.get(page_path).status_code == 200
    assert draft['title'] not in client.get('/workbench?view=trash').text
    assert client.get(api_path).json()['sources'] == draft['sources']
    with Session(app.state.engine) as s:
        assert official_detail(s, '456')['event']['selected_task'] == key
        assert _selected(s, ['source-topic']) == {'source-topic': key}


def test_delete_refuses_committed_advance_and_pending_handoff(research):
    app, client, _ = research
    draft = create_draft(client, '已有研究计划', 'trash-test-stage')
    key = draft['key']
    url = f'/api/workbench/{key}'
    assert client.post(url + '/phase', json={'version': 1, 'phase': 'research'}).status_code == 200
    assert client.delete(url, params={'version': 0}).status_code == 409
    assert 'data-work-trash="remove"' not in client.get(f'/workbench/{key}/selection').text

    pending = create_draft(client, '交接处理中', 'trash-test-pending')
    key = pending['key']
    with Session(app.state.engine) as s:
        s.add(TopicHandoff(handoff_id='test-handoff', workspace_id=key, request_hash='test',
            payload_json='{"action":"deep"}', case_id='test-research'))
        s.commit()
    assert client.delete(f'/api/workbench/{key}', params={'version': 0}).status_code == 409
    assert pending['title'] not in client.get('/workbench?view=trash').text
