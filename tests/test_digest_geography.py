"""Selected-card reading groups retain their archive evidence and never change rank."""
from copy import deepcopy

from news2douyin.digest import geography
from news2douyin.intake.lake import Lake
from test_intake_projection import line
from test_intake_storage_v2 import STAMP, legacy, raw_zip


def gkg_card(index):
    return dict(key=f'selected-{index}', title='A saved headline', rank=index,
                country='', channel='', source_kind='gkg', source_key=f'gkg:gkg_english:{STAMP}:{index}',
                source_url=f'https://media.example/article/{index}')


def test_selected_groups_read_only_the_exact_selected_documents(tmp_path, monkeypatch):
    def article(index, places='', themes=''):
        cells = line(STAMP, index, 'A saved headline').rstrip('\n').split('\t')
        cells[3] = 'unrelated-publisher.fr'
        cells[7] = ''
        cells[8] = themes
        cells[10] = places
        return ('\t'.join(cells) + '\n').encode()

    raw = article(1, '4#Beijing#CH#CH22#123#39#113#1#99;4#Paris#FR#FR11#123#48#2#2#40;'
                     '4#Beijing#CH#CH22#123#39#113#1#6;4#Paris#FR#FR11#123#48#2#2#40',
                  'ELECTION,55;MEDICAL,23;MEDICAL,122')
    raw += article(2, themes='TAX_FNCACT,55') + article(3) + article(4, themes='MEDICAL,3')
    lake = Lake(tmp_path)
    lake.ingest_archive('gkg_english', STAMP, raw_zip(tmp_path, raw))
    with lake.connection() as db:
        db.execute("UPDATE intake_records_v2 SET raw_sha256='bad' WHERE row_index=4")

    decoded = []
    material = Lake.material

    def track(row):
        decoded.append(row['row_index'])
        return material(row)

    monkeypatch.setattr(Lake, 'material', staticmethod(track))
    selected = [gkg_card(1), gkg_card(2), gkg_card(4)]
    mismatch = dict(gkg_card(1), key='another', source_url='https://different.example/fr')
    selected.append(mismatch)
    before = deepcopy(selected)
    geography.enrich_selected(selected, storage_root=tmp_path)
    assert sorted(decoded) == [1, 2, 4]  # one decode per distinct selection, including the corrupt row
    result = selected[0]
    assert result['geography'] == dict(countries=['CN', 'FR'], primary='CN', basis='gkg_mentions',
                                       source_key=result['source_key'])
    assert result['category'] == dict(label='健康与医疗', basis='gkg_theme', theme='MEDICAL', source_key=result['source_key'])
    for card in selected[1:]:
        assert 'geography' not in card and 'category' not in card
    assert [{k: c[k] for k in orig} for c, orig in zip(selected, before)] == before


def test_legacy_unordered_multicountry_ties_and_single_country(tmp_path):
    raw = line(STAMP, 1, 'Saved legacy headline').encode()
    legacy(tmp_path, raw, extra=dict(mention_iso=['us', 'xx', 'pl', 'US'], themes=['ELECTION', 'MEDICAL']))
    card = gkg_card(1)
    geography.enrich_selected([card], storage_root=tmp_path)
    assert card['geography']['countries'] == ['PL', 'US']
    assert card['geography']['primary'] == '' and 'category' not in card
    assert geography._place_group(dict(mention_iso=['pl']))['primary'] == 'PL'
    assert geography._category(dict(themes=['ELECTION']))['label'] == '政治与治理'
    assert geography._place_group(dict(mention_iso=123, enhanced_locations=[])) == {}
    assert geography._category(dict(themes=[{}, 'UNKNOWN'], enhanced_themes=[])) == {}


def test_event_location_remains_distinct_from_publisher_country(tmp_path):
    official = dict(key='event', source_kind='gdelt', source_key='gdelt:123', country='pl', channel='磋商与接触')
    missing = dict(key='old-card', title='Headline')
    platform = dict(key='platform', source_kind='newsnow', country='cn', language='zh')
    source = gkg_card(5)
    cards = [platform, missing, source, official]
    geography.enrich_selected(cards, storage_root=None)
    geography.enrich_selected(cards, storage_root=tmp_path)
    assert official['geography'] == dict(countries=['PL'], primary='PL', basis='official_action', source_key='gdelt:123')
    assert official['channel'] == '磋商与接触'
    assert all('geography' not in c for c in cards[:3])
    assert not Lake(tmp_path).path.exists()
