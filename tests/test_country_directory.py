from fastapi.testclient import TestClient

from news2douyin.server.app import create_app
from news2douyin.server.countries import directory_context


def test_country_catalog():
    data = directory_context()
    rows = data['countries']
    assert len(rows) == 249
    for key, length in [('a2', 2), ('a3', 3), ('num', 3)]:
        assert len({row[key] for row in rows}) == len(rows)
        assert all(len(row[key]) == length for row in rows)
    assert {row['continent'] for row in rows} == set(data['continents'])
    assert all(row['zh'] and row['en'] and (row['capitals'] or row['note']) for row in rows)
    by_code = {row['a2']: row for row in rows}
    assert by_code['PL']['capitals'] == [['华沙', 'Warsaw']]
    assert by_code['AF']['num'] == '004'
    assert by_code['AX']['initial'] == 'A'
    assert by_code['CY']['continent'] == '亚洲'
    assert by_code['MX']['continent'] == '北美洲'
    assert by_code['AQ']['capitals'] == []
    assert '行政中心' in by_code['NR']['note']
    assert by_code['GQ']['capitals'][0][1] == 'Ciudad de la Paz'
    assert len(by_code['ZA']['capitals']) == 3


def test_resource_route_and_collection_entry(tmp_path):
    app = create_app(db_url=f'sqlite:///{tmp_path}/app.db', storage_root=str(tmp_path))
    try:
        client = TestClient(app)
        response = client.get('/country-reference.html')
        assert client.get('/resources/countries').content == response.content
        assert response.status_code == 200
        assert response.text.count('<tr data-country=') == 249
        assert '按字母 A–Z' in response.text and '按洲' in response.text
        assert 'href="/country-reference.html"' in client.get('/topics').text
        assert 'href="/country-reference.html" target="_blank"' in client.get('/profiles').text
    finally:
        app.state.engine.dispose()
