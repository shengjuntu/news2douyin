from datetime import date, timedelta
import json

import pytest

from news2douyin.digest.publisher_comparison import compare_publishers, format_comparison_html, write_comparison
from news2douyin.digest.ranking import Candidate


def test_compare_replays_policies_once_per_day_and_explains_old_only_card(tmp_path):
    first = date(2025, 3, 18)
    def card(i, title, host, day):
        return Candidate(key=f'feed:{i}', title=title, observed_at=str(day)+'T03:00:00Z',
                         source_url=f'https://{host}/article', source_kind='gkg')
    network = ['wflanews.iheart.com','newstalk1130.iheart.com','wgy.iheart.com','wrko.iheart.com']
    calls = []
    def candidates(day, stats):
        calls.append(day)
        stats['gkg_archive_present'] = True
        if day == first:
            for i, host in enumerate(network):
                yield card(i, 'Theater unveils its summer season', host, day)
            for i, host in enumerate(['first.example','second.example'], len(network)):
                yield card(i, 'Harbor board unveils a summer schedule', host, day)
    report = compare_publishers(first, first+timedelta(days=1), candidates, warmup=1, limit=1)
    assert calls == [first-timedelta(days=1), first, first+timedelta(days=1)]
    today = report['days'][str(first)]
    assert (today['common'],today['entered'],today['left'])==(0,1,1)
    added, left = today['changes']
    assert (added['status'],added['old_rank'],added['new_rank']) == ('entered',None,1)
    assert (left['old_rank'],left['new_rank'],left['raw_hosts'],left['publisher_breadth']['units'])==(1,None,4,1)
    assert report['days'][str(first+timedelta(days=1))]['new_selected']==0
    write_comparison(report, tmp_path/'r.json')
    assert json.loads((tmp_path/'r.json').read_text())==report
    with pytest.raises(FileExistsError):
        write_comparison(report, tmp_path/'r.json')
    left.update(title='<img onerror=bad()>',url='javascript:bad()')
    html = format_comparison_html(report)
    assert '&lt;img onerror=bad()&gt;' in html and 'href="javascript' not in html
    assert '报道域名 4 · 排序计数 1' in html


def test_unchanged_rank_can_show_corrected_count():
    day = date(2025, 3, 19)
    def rows(_day, _counts):
        for n,h in enumerate(['wgy.iheart.com','wrko.iheart.com']):
            yield Candidate(key=str(n), title='Museum opens an exhibit for spring', observed_at='2025-03-19T06:00:00Z',
                            source_url=f'https://{h}/museum',source_kind='gkg')
    result = compare_publishers(day,day,rows,warmup=0)['days'][str(day)]
    assert result['same_position'] == 1 and result['changes'][0]['status'] == 'same'
