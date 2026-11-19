from hashlib import sha256
import json
from pathlib import Path

import pytest

from news2douyin.digest.publisher_families import IHEART_HOSTS, MAP_VERSION, NBC_LOCAL_HOSTS, publisher_unit


def test_reference_directory_snapshot_and_regression_case():
    assert MAP_VERSION == '2026-11-15.v1'
    evidence_path = Path(__file__).resolve().parents[1] / 'docs/validation/0.34.0/publisher-directory.json'
    evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
    assert evidence['version'] == MAP_VERSION
    assert sha256(('\n'.join(sorted(IHEART_HOSTS)) + '\n').encode('ascii')).hexdigest() == (
        evidence['iheart']['snapshot_hosts_sha256'])
    assert set(NBC_LOCAL_HOSTS) == {row['host'] for row in evidence['nbc_local']['verified_stations']}
    assert len(IHEART_HOSTS) == 879
    assert len(NBC_LOCAL_HOSTS) == 11
    for domain in ('1360kktx', '590kqnt', 'alt1073', 'jetradio1400', 'wccfradio'):
        assert publisher_unit(domain + '.iheart.com') == ('family:iheart', 'iHeartMedia')
    assert publisher_unit('www.NBCMIAMI.COM.:443') == publisher_unit('NBCConnecticut.com')
    assert publisher_unit('nbcmiami.com') == ('family:nbc-local', 'NBCUniversal Local')


@pytest.mark.parametrize('host', [
    'unknown-station.iheart.com', 'wgy.iheart.com.evil.example', 'evilwgy.iheart.com',
    'other.wgy.iheart.com', 'notnbcmiami.com', 'affiliate.nbcnewyork.com',
    'iheart.com', 'iheartmedia.com', 'nbc.com', 'nbcnews.com', 'wthr.com',
    'reader-a.wordpress.com', 'reader-b.wordpress.com', 'city-a.example.co.uk',
    'city-b.example.co.uk', 'xn--bcher-kva.ch', '192.0.2.1',
])
def test_unmapped_sites_preserve_exact_host_and_no_family_label(host):
    assert publisher_unit(host) == ('host:' + host, '')


def test_equivalent_unknown_host_spellings_and_ports():
    assert publisher_unit(' WWW.Bücher.ch.:8443 ') == publisher_unit('xn--bcher-kva.ch')
    assert publisher_unit('[2001:db8:0::1]:443') == publisher_unit('2001:db8::1')


@pytest.mark.parametrize('host', [
    '', None, ' ', 'https://nbcmiami.com', '//wgy.iheart.com', 'nbcmiami.com/path',
    'nbcmiami.com?story=1', 'wgy.iheart.com@other.example', 'unknown..example',
    'nbcmiami.com:65536', 'nbcmiami.com:0', 'nbcmiami.com:abc', 'nbcmiami.com..',
    '[::1]@nbcmiami.com', 'bad:ipv6:literal', 'a'*64 + '.com',
    'a'*5000, 'evil\\nbcmiami.com', 'local\nmedia.example',
])
def test_malformed_input_has_no_count_or_attribution(host):
    assert publisher_unit(host) == ('', '')
