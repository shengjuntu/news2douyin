"""Conservative, versioned identity for news radar headlines."""
import hashlib
import re
import unicodedata
from urllib.parse import urlsplit

from ..dedup.normalize import canonicalize_url
from ..selection.source_content import usable_title

_NAVIGATION = {'breaking news', 'latest news', 'live updates', 'news updates', 'world news',
               '报道内容待补充', '最新消息', '新闻动态', '新闻快讯', '突发新闻', '最新进展'}
# Only remove explicit wire attributions after a delimiter. Do not strip an
# arbitrary parenthesis (it may be a quantity), question, negation or sign.
_WIRE_SUFFIX = re.compile(r'\s+(?:[|–—-]\s*(?:reuters|associated press|afp|新华社)|\(reuters\))$', re.I)
# Several jurisdictions can publish exactly this headline on the same day.
# A verified common document is safer than unifying cases by a generic role.
_ROLE_HEADLINE = re.compile(
    r'(?:(?:the )?(?:president|prime minister|minister|governor|mayor) '
    r'(?:resigns?|resigned|steps down|announces resignation|wins election)|'
    r'(?:总统|总理|首相|部长|市长)(?:辞职|宣布辞职|当选|宣布卸任))$'
)
_PUNCT = str.maketrans({'“': '"', '”': '"', '’': "'", '‘': "'", '–': '-', '—': '-'})


# Only typographically marked publisher credits. Words like "Reuters says",
# live/update numbers and attribution in running text may change the claim.
_CREDIT = (r'reuters|associated press|ap news|agence france-presse|afp|bbc news|'
           r'新华社|新华网|路透社|中国新闻网')
_CREDIT_START = re.compile(rf'^(?:(?:\((?:{_CREDIT})\)|【(?:{_CREDIT})】|\[(?:{_CREDIT})\])\s*|(?:{_CREDIT})\s+\|\s+)')
_CREDIT_END = re.compile(rf'(?:\s*\|\s*(?:{_CREDIT})|\s+-\s+(?:{_CREDIT})|\s+\((?:{_CREDIT})\))$')
# English legal commencement phrases, with tense preserved. Require a named
# instrument type and an explicit number in the same headline. Distinct
# subjects, direction, stages, quantities, jurisdictions and dates all survive.
_INSTRUMENT = re.compile(r'\b(?:act|regulation|directive|tariffs?|treaty|agreement|ban|law|rules?)\b')
_COMMENCEMENT = re.compile(r'\b(comes? into force|came into force|takes? effect|took effect)\b')
_COMMENCEMENT_FORM = {
    'comes into force': 'enters into force', 'takes effect': 'enters into force',
    'come into force': 'enter into force', 'take effect': 'enter into force',
    'came into force': 'entered into force', 'took effect': 'entered into force',
}


# These publisher networks put the full title slug in the article URL. Some
# pages append the affiliate's masthead to that title. Check both bits of
# evidence before dropping a suffix. A pipe/dash alone can introduce news.
_FREQUENCY = r'\d{2,4}(?:\.\d{1,2})?(?:\s*(?:am|fm))?'
_STATION = re.compile(
    rf'(?:(?:news ?radio|news/talk|fox sports|country) )?'
    rf'(?:{_FREQUENCY}(?:\s*(?:and|[&/])\s*{_FREQUENCY})? )?[kw][a-z]{{2,3}}(?:[- ](?:am|fm))?')
_NBC_AFFILIATES = {
    'nbcbayarea.com': 'nbc bay area', 'nbcboston.com': 'nbc boston',
    'nbcchicago.com': 'nbc chicago', 'nbcconnecticut.com': 'nbc connecticut',
    'nbcdfw.com': 'nbc 5 dallas-fort worth', 'nbclosangeles.com': 'nbc los angeles',
    'nbcmiami.com': 'nbc 6 south florida', 'nbcnewyork.com': 'nbc new york',
    'nbcphiladelphia.com': 'nbc10 philadelphia', 'nbcsandiego.com': 'nbc 7 san diego',
    'nbcwashington.com': 'nbc4 washington',
}
_RADIO_ARTICLE = re.compile(r'/content/\d{4}-\d{2}-\d{2}-([a-z0-9-]+)/?')
_NBC_ARTICLE = re.compile(r'/news/(?:[a-z0-9_-]+/)*([a-z0-9-]+)/[0-9]+/?')


def _affiliate_title(name: str, url: str) -> str:
    # Taking the last marked separator keeps embedded subtitles and negatives.
    pos = max(name.rfind(' | '), name.rfind(' - '))
    if pos == -1:
        return name
    core, credit = name[:pos], name[pos + 3:]
    target = urlsplit(url)
    host = (target.hostname or '').lower().removeprefix('www.')
    if host.endswith('.iheart.com') and _STATION.fullmatch(credit):
        article = _RADIO_ARTICLE.fullmatch(target.path)
    elif _NBC_AFFILIATES.get(host) == credit:
        article = _NBC_ARTICLE.fullmatch(target.path)
    else:
        return name
    if article:
        words = re.findall(r'[a-z0-9]+', core)
        if len(words) >= 4 and '-'.join(words) == article[1]:
            return core
    return name


def _headline_identity(name: str, url: str) -> str:
    if _ROLE_HEADLINE.fullmatch(name):
        name += '\0' + canonicalize_url(url)
    return 'progress_' + hashlib.sha256(name.encode()).hexdigest()[:40]


def progress_keys(title: str, url: str) -> tuple[str, str]:
    name = unicodedata.normalize('NFKC', title).casefold().translate(_PUNCT)
    name = _WIRE_SUFFIX.sub('', ' '.join(name.split())).rstrip('。.')
    if len(name) < 4 or name in _NAVIGATION or not usable_title(name):
        return '', ''
    previous = _headline_identity(name, url)
    current = name
    if current.startswith(('(', '[', '【')) or '|' in current or ' - ' in current or current.endswith(')'):
        current = _CREDIT_END.sub('', _CREDIT_START.sub('', current)).strip().rstrip('。.')
        current = _affiliate_title(current, url)
    if ('effect' in current or 'force' in current) and any(c.isdecimal() for c in current) and _INSTRUMENT.search(current):
        current = _COMMENCEMENT.sub(lambda match: _COMMENCEMENT_FORM[match[0]], current)
    if current == name or len(current) < 8 or current in _NAVIGATION or _ROLE_HEADLINE.fullmatch(current) or not usable_title(current):
        return previous, previous
    return previous, _headline_identity(current, url)


def card_progress_key(card: dict) -> str:
    """Map an old published card using only the headline and URL saved with it."""
    title, url = card.get('title'), card.get('source_url')
    if title and url:
        _, current = progress_keys(title, url)
        if current:
            return current
    return card['key']
