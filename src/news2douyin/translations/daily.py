"""Bind a reading translation to the material displayed in a daily edition."""
from datetime import date
from hashlib import sha256
import json
import re

from sqlalchemy import select

from ..digest.models import DailyDigestIssue
from ..selection.source_content import source_url
from ..tasks.control import TaskConflict
from .language import describe_language


def _hash(value):
    encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return sha256(encoded).hexdigest()


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def _card_material(card):
    title = _text(card.get('title'))
    excerpt = _text(card.get('excerpt'))
    address = source_url(card.get('source_url'))
    action = card.get('action_evidence') or {}
    quote = _text(action.get('quote')) if address and action.get('url') == address else ''
    if quote and quote != title:
        excerpt = quote
    if excerpt == title:
        excerpt = ''
    reconstruction = card.get('reconstruction') or {}
    declared = _text(card.get('language')) or _text(reconstruction.get('language'))
    return title, excerpt, declared, address


def _key(card):
    return _text(card.get('key')) or 'legacy_' + _hash(_card_material(card))


def daily_card_descriptor(day, card):
    title, excerpt, declared, source_url = _card_material(card)
    if not title and not excerpt:
        return None
    language = describe_language('\n\n'.join([title, excerpt]), declared)
    # A mixed-language or Traditional Chinese card can still need Simplified Chinese.
    simplified = (language['basis'] == 'declared' and language['code'] in ('zh', 'zho', 'zh-cn', 'zh-hans', 'zh-sg')
                  and all(describe_language(text)['code']=='zh' for text in (title, excerpt) if text))
    return dict(day=str(day), key=_key(card), fingerprint=_hash((title, excerpt, declared, source_url)),
                already_chinese=bool(simplified), likely_chinese=language['basis']=='script' and language['code']=='zh',
                has_excerpt=bool(excerpt), has_title=bool(title))


def resource_key(day, key, fingerprint):
    try:
        parsed = date.fromisoformat(day)
    except (TypeError, ValueError):
        raise ValueError('日报日期无效') from None
    if parsed.isoformat() != day:
        raise ValueError('日报日期无效')
    if not re.fullmatch(r'[0-9a-f]{64}', fingerprint):
        raise ValueError('日报内容指纹无效')
    return f'{day}/{_hash(key)}/{fingerprint}'


def source_material(card, descriptor):
    """Use the same input bounds for submission and read-only cache lookup."""
    from .service import MAX_TITLE, MAX_BODY

    title, excerpt, declared, _ = _card_material(card)
    original = '\n\n'.join(text for text in (title[:MAX_TITLE], excerpt[:MAX_BODY]) if text)
    language = describe_language(original, declared)
    language['already_chinese'] = descriptor['already_chinese']
    scope = ('document' if title else 'content') if excerpt else 'title'
    return original, language, len(title) > MAX_TITLE or len(excerpt) > MAX_BODY, scope, None


def saved_text(session, key):
    day, card_id, fingerprint = key.split('/')
    # A replacement can be in progress while the last successful items remain readable.
    items = session.execute(select(DailyDigestIssue.items_json).where(DailyDigestIssue.day == day)).scalar_one_or_none()
    if items is None:
        raise KeyError('这一天没有已保存的日报')
    for card in json.loads(items):
        if not isinstance(card, dict) or _hash(_key(card)) != card_id:
            continue
        descriptor = daily_card_descriptor(day, card)
        if not descriptor or descriptor['fingerprint'] != fingerprint:
            continue
        return source_material(card, descriptor)
    raise TaskConflict('日报已更新，这条卡片的材料已变化或移出。请刷新日报后再译。')
