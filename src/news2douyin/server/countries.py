"""Read-only geographic reference shared by the WebUI and the offline edition."""
import json
from functools import lru_cache
from pathlib import Path
from unicodedata import normalize

CONTINENTS = ('亚洲', '欧洲', '非洲', '北美洲', '南美洲', '大洋洲', '南极洲')


@lru_cache(maxsize=1)
def directory_context():
    path = Path(__file__).with_name('data') / 'countries.json'
    countries = json.loads(path.read_text(encoding='utf-8'))
    for row in countries:
        row['initial'] = normalize('NFKD', row['en'][0])[0].upper()
    return {'countries': countries, 'continents': CONTINENTS}
