import csv
import json
import re

from news2douyin.digest.review_page import write_review_page


def test_offline_sheet_keeps_blind_fields_and_untrusted_headlines_inside_json(tmp_path):
    row = dict(id='a001', day='2025-03-16', title='</script><img onerror="alert(1)">',
               excerpt='报道 & 进展', observed_at='', source_url='javascript:alert(1)',
               material_origin='报道', important='', fact_group='', note='',
               internal_ranking='private answer marker')
    with (tmp_path / 'review.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=row)
        writer.writeheader()
        writer.writerow(row)
    write_review_page(tmp_path)
    html = (tmp_path / 'review.html').read_text(encoding='utf-8')
    assert 'private answer marker' not in html and row['title'] not in html
    payload = re.search(r'<script id="blind-data" type="application/json">(.*?)</script>', html).group(1)
    assert json.loads(payload)[0]['title'] == row['title']
    assert 'answer-key' not in html
