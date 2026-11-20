from datetime import date
from urllib.parse import parse_qs, urlsplit

from news2douyin.digest.video_story import focus_url


def test_archived_story_link_can_prefill_a_focus():
    selected = '2026-11-19'
    card = {'key': 'later & current'}
    href = urlsplit(focus_url(card, day=date.fromisoformat(selected)))
    assert href.path == '/focus'
    assert parse_qs(href.query) == {'new': ['1'], 'video_day': [selected], 'video_key': [card['key']]}
    assert not focus_url(card, day='2026-11-31')
    assert not focus_url({'key': ''}, day=selected)
