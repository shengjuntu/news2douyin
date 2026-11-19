"""Freeze the saved edition's actual source samples for an editor's selection."""
from datetime import date
import hashlib
import json
import re
from urllib.parse import urlsplit

from ..research import service as research
from ..research.materials import reconstruction_lead
from ..selection.service import _url
from ..topics.models import TopicSource
from ..tasks.control import TaskConflict
from . import service


def gkg_ref(source_key):
    match = re.fullmatch(r'gkg:(gkg_english|gkg_translation):(\d{14}):([1-9]\d{0,8})', source_key)
    if match:
        feed, stamp, index = match.groups()
        return dict(feed=feed, stamp=stamp, row_index=int(index))
    return {}


def can_select_card(card):
    return bool(card.get('key') and card.get('title') and (
        _url(card.get('source_url')) or any(_url(sample.get('url')) for sample in card.get('sources') or [])))


def card_token(card):
    """Identify the saved materials a reader would actually take to the workbench."""
    fields = ('key', 'title', 'excerpt', 'source_key', 'source_kind', 'source_url',
              'sources', 'reconstruction', 'official_coverage', 'action_evidence', 'country')
    material = {name: card.get(name) for name in fields}
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode()).hexdigest()


def selected_card(session, day, progress, token=''):
    try:
        parsed_day = date.fromisoformat(day)
    except (ValueError, TypeError):
        raise ValueError('日报日期无效') from None
    if parsed_day.isoformat() != day or not progress:
        raise ValueError('请选择一期日报中的进展')
    issue = service.read_issue(session, parsed_day)
    if issue['status'] == 'ready':
        for card in issue['items']:
            if card.get('key') == progress:
                if token and token != card_token(card):
                    raise TaskConflict('日报已更新，这条进展的来源材料已变化。请返回日报重新选择。')
                if not can_select_card(card):
                    raise ValueError('这条历史线索没有可带入选题的来源')
                return card
    if token:
        raise TaskConflict('日报已更新，这条进展已离开本期清单。请返回日报重新选择。')
    raise KeyError('这期日报没有这条进展')


def freeze_sources(session, workspace, day, card):
    """Use the same title/excerpt and evidence URLs the published edition stores."""
    representative = dict(url=card.get('source_url'), title=card['title'], excerpt=card.get('excerpt') or '',
                          source_kind=card.get('source_kind'), source_key=card.get('source_key'),
                          reconstruction=card.get('reconstruction'))
    selected = []
    seen = set()
    for sample in [representative, *(card.get('sources') or [])]:
        url = _url(sample.get('url'))
        reconstruction = sample.get('reconstruction')
        identity = (url, bool(reconstruction))
        if not url or identity in seen:
            continue
        seen.add(identity)
        source_key = sample.get('source_key') or ''
        kind = sample.get('source_kind') or ''
        document = dict(provider=kind, title=sample.get('title') or url, content=sample.get('excerpt') or '',
                        url=url, canonical_url=url, published_at='', revision=None, language='',
                        source_domain=urlsplit(url).hostname, country=card.get('country') or '',
                        source_kind='search_snippet', material_kind='source_excerpt', requires_original_text=True,
                        radar_day=day, radar_progress=card['key'], radar_source_key=source_key)
        if kind == 'newsnow':
            document.update(source_kind='newsnow_metadata', attention_topic=source_key.partition(':')[2])
        elif kind == 'gdelt' and source_key.partition(':')[2].isdigit():
            document['official_event_id'] = source_key.partition(':')[2]
            from ..desk.official_graph import track
            track(session, document['official_event_id'])
        elif kind == 'gkg':
            document['source_ref'] = gkg_ref(source_key)
        if reconstruction:
            document.update(reconstruction_lead(reconstruction))
        if sample is representative and card.get('official_coverage'):
            document['official_coverage'] = card['official_coverage']
        if sample is representative and card.get('action_evidence'):
            document['action_evidence'] = card['action_evidence']
        source_id_parts = [workspace, card['key'], url]
        if reconstruction:
            source_id_parts.append('gdelt_web_ngrams')
        sid = 'topic_source_' + research.digest(source_id_parts)[:24]
        session.add(TopicSource(source_id=sid, workspace_id=workspace, article_key='radar_daily/'+sid,
                                document_json=research.dump(document), content_hash=research.digest(document)))
        selected.append(sid)
        if len(selected) == 12:
            break
    if not selected:
        raise ValueError('这条进展没有可带入工作台的来源链接')
    return selected
