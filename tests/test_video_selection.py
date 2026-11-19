"""Video selection keeps the ordinary Daily Top N and its evidence independent."""
from dataclasses import replace

from news2douyin.digest.candidate_pool import Candidate, grouped_candidates
from news2douyin.digest.video_selection import rank_video, select_video


def _row(key, title, **values):
    return Candidate(f'report:{key}', title, '2026-11-17T09:00:00Z',
                     values.pop('source_url', f'https://report.example/{key}'),
                     values.pop('source_kind', 'report'), **values)


def test_video_surfaces_an_ordinary_person_with_a_story_without_changing_daily():
    diplomatic = _row('summit', 'International delegates discuss the border and tariff treaty',
                      impact=1.0, is_primary=True, domains=tuple(f'wire{i}.test' for i in range(30)))
    local = _row('route', '公交司机送饭给独居老人，后来同事接力',
                 excerpt='司机下班后绕行送来热饭。邻居随后也来帮忙。')
    with grouped_candidates([diplomatic, local], keep_baseline=False) as pool:
        daily = pool.select(limit=1)
        video = rank_video(pool, limit=1)
        assert daily['items'][0]['title'] == diplomatic.title
        assert pool.select(limit=1) == daily
    assert video['stats']['scanned'] == 2
    card, = video['items']
    assert card['title'] == local.title
    assert card['video']['evidence'] == dict(status='excerpt', url=local.source_url, source_key=local.key)
    assert card['video']['person']['text'] == '司机'
    assert '前后变化' in card['video']['movement']['text']
    assert '接力' in card['video']['movement']['quote']
    for kind in ('person', 'movement', 'life'):
        clue = card['video'][kind]
        assert clue['quote'] in getattr(local, clue['field'])


def test_life_change_and_reported_story_outrank_promised_change_or_reconstruction():
    rows = [
        _row('confirmed', 'A tenant moved out after the landlord raised her rent',
             excerpt='The tenant received a new lease and moved into her sister’s spare room.'),
        _row('next', 'A tenant might move out after rent rises'),
        _row('bait', 'You won’t believe this story about the bus driver'),
        _row('coded', 'New port rules take effect', actors=('NOT A PERSON',), source_kind='gdelt'),
        _row('rebuilt', 'A tenant received a refund after the landlord raised the rent', source_kind='gdelt',
             reconstruction={'note': 'reassembled from fragments'}, excerpt='A reconstructed excerpt'),
        _row('fact-without-person', '公交票价下降', excerpt='市里降价后，通勤月票的价格减少。'),
        _row('roundup', '5 things to know for March 18: bus drivers returned after the school closed'),
    ]
    video = select_video(rows)
    assert [card['source_key'] for card in video['items']] == ['report:confirmed', 'report:fact-without-person']
    assert 'person' not in video['items'][1]['video']
    assert video['stats']['insufficient_story_evidence'] == 5


def test_spanish_and_source_bound_headline_markers():
    spanish = _row('school', 'La maestra regresó a la escuela después de que vecinos la abrieron')
    card, = select_video([spanish])['items']
    assert card['video']['person']['quote'] == spanish.title
    assert card['video']['movement']['text'] == '有前后变化或应对经过'
    assert card['video']['life']['text'] == '教育与照护'
    assert card['video']['evidence']['status'] == 'headline'
    assert card['reasons'][-1] == '标题线索，制作前核对原文'


def test_video_scans_full_day_applies_saved_keys_and_caps_unique_urls():
    first = _row('same', '护士下班后送饭给邻居', source_url='https://report.example/social?id=7')
    duplicate = replace(first, title='护士先帮邻居买菜后来帮忙送饭', key='report:dup',
                        source_url='https://report.example/social?id=7&utm_source=site')
    results = select_video([first, duplicate])
    assert results['stats']['duplicate_source_groups'] == 1
    assert len(results['items']) == 1
    chosen = results['items'][0]
    assert chosen['title'] == duplicate.title
    tracked = select_video([first, duplicate], prior_keys=[chosen['key']])
    assert tracked['stats']['prior_excluded'] == 1
    rows = [_row(str(n), f'在第 {n} 号站，司机停运后送饭，同事接力') for n in range(180)]
    many = select_video(iter(rows), limit=100)
    assert many['stats']['scanned'] == len(rows)
    assert len(many['items']) == 100
    assert many == select_video(reversed(rows), limit=100)
    assert len(select_video(rows, limit=150)['items']) == 150


def test_activity_and_documented_outcomes_beat_graphic_allegations_and_advice():
    rows = [
        _row('advice', "Schools must nurture innovative, entrepreneurial mindset to help students' future success"),
        _row('abuse', "US Teacher, 30, Charged With Sex Abuse After Teen's Mother Finds Explicit Texts"),
        _row('abuse-alias', "Female teacher faces felony charges after teen's mom finds explicit texts"),
        _row('injury', 'Young worker found dead after a factory fired him; his family received a letter'),
        _row('rescue', 'Fire crews found passengers alive and rescued them after the ferry stopped'),
        _row('return', 'Students return to class for first time since deadly Oregon school shooting'),
        _row('care', 'Families received free tuition after students raised living costs with Harvard staff'),
    ]
    result = select_video(rows)
    chosen = [card['source_key'] for card in result['items']]
    assert all(item not in chosen for item in ('report:advice', 'report:abuse', 'report:abuse-alias'))
    assert chosen.index('report:care') < chosen.index('report:injury')
    assert chosen.index('report:rescue') < chosen.index('report:injury')
    assert 'report:return' in chosen


def test_only_explicit_headline_section_labels_create_a_repeat_alias():
    rows = [
        _row('wire', 'A bus driver restored school service after parents asked for help'),
        _row('section', 'A bus driver restored school service after parents asked for help | Local News'),
        _row('different', 'A bus driver restored school service after parents asked for help | Montreal'),
        _row('new-stage', 'A bus driver canceled school service after parents asked for help | Local News'),
    ]
    result = select_video(rows)
    assert result['stats']['duplicate_title_groups'] == 1
    keys = {card['source_key'] for card in result['items']}
    assert len(keys) == 3 and {'report:different','report:new-stage'} <= keys
