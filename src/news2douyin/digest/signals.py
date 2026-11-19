"""Conservative editorial cues independent of volume and current hotlists.

A headline matching an action is a lead for the reader, not verification.
A source qualifies as an original document only under a known issuer path.
"""
import re
from urllib.parse import urlsplit

from ..desk.rules import CHANGE, IMPACT

# Actual document collections; a government homepage or news article alone is
# insufficient to identify a primary document. Do not infer the location of
# the reported event or the reader's country from these publishers.
_DOCUMENTS = {
    'eur-lex.europa.eu': re.compile(r'^/(?:eli/|legal-content/[A-Za-z]{2}/TXT/)', re.I),
    'federalregister.gov': re.compile(r'^/documents/\d{4}/\d{2}/\d{2}/', re.I),
    'gov.cn': re.compile(r'^/zhengce/(?:zhengceku/|content/|content_)', re.I),
    'legislation.gov.uk': re.compile(r'^/(?:uksi|ukpga|ukla)/\d{4}/\d+/', re.I),
}
# Material policy stages not captured in the older radar title rules.
_STAGE = re.compile(
    r'正式生效|开始实施|表决通过|公布.{0,12}(?:新规|禁令)|撤销.{0,12}(?:限制|禁令)|'
    r'\b(?:takes? effect|enters? into force|comes? into force|ratifies|ratified|'
    r'lifts? (?:the )?(?:ban|restrictions)|'
    r'entre en vigueur|entra en vigor|entra em vigor|tritt in kraft)\b', re.I)
_SUBJECT = re.compile(
    r'出口(?:管制|限制)|进出口|(?:暂停|恢复)通航|紧急状态|'
    r'\b(?:export (?:controls?|restrictions?|licen[cs]es?)|state of emergency)\b', re.I)


def headline_signals(title: str, url: str) -> tuple[float, bool]:
    """Return an explainable screening signal in [0, 1] and issuer provenance."""
    impact = bool(IMPACT.search(title) or _SUBJECT.search(title))
    change = bool(CHANGE.search(title) or _STAGE.search(title))
    target = urlsplit(url)
    host = (target.hostname or '').lower().removeprefix('www.')
    path_rule = _DOCUMENTS.get(host)
    original = bool(path_rule and path_rule.search(target.path))
    return (2 * int(impact) + int(change)) / 3, original


# These are cues for editorial review, scoped to the source passage. A coded
# event date, a first-seen identifier or growing coverage cannot supply them.
_REALIZED = re.compile(
    r'表决通过|签署了|已签署|已(?:正式)?生效|正式生效|开始实施|已(?:正式)?批准|'
    r'\b(?:signed|ratified|enacted|came into force|entered into force|took effect|'
    r'approved|lifted the ban|opened for traffic)\b', re.I)
_UNRESOLVED = re.compile(
    r'可能|或将|拟议|准备|计划|预计|如果|否认|尚未|没有|并未|未获|未被|此前|去年|'
    r'[?？]|据称|声称|被指|辟谣|回顾|周年|明年|即将|将于|尚待|撤回传言|'
    r'\b(?:will|would|may(?!\s+(?:\d{1,2}\b|\d{4}\b))|might|could|plans?|planned|propos(?:e|ed|al)|if|'
    r'expects?|expected|den(?:y|ies|ied)|not|never|last year|last month|previously|whether|'
    r'upcoming|up for approval|to be|yet to be|has yet to|allegedly|claims?|rumou?rs?|'
    r'anniversary|years? ago|retrospective|looking back)\b|n[’\']t\b', re.I)
# A year without its month/day is insufficient to date a claimed action.
_YEAR = re.compile(r'(?<!\d)(?:19|20)\d{2}(?!\d)')
# Do not join an edition masthead's date or a publisher credit to an
# unrelated action in a second headline, pull quote or appended subtitle.
_TITLE_SECTIONS = re.compile(r'\s+[|｜—–]\s+|\s+[-:]\s+|[：:]\s+')
_DATE = re.compile(r'(?<!\d)(\d{4})[-年](\d{1,2})[-月](\d{1,2})(?:日)?(?!\d)')


_MONTHS = ('january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
           'september', 'october', 'november', 'december')
_MONTH_NUMBERS = {alias: n for n, name in enumerate(_MONTHS, 1) for alias in (name, name[:3])}
_MONTH_NUMBERS['sept'] = 9
_MONTH_WORD = '(?:' + '|'.join(sorted(_MONTH_NUMBERS, key=len, reverse=True)) + ')'
_MONTH_FIRST = re.compile(rf'\b({_MONTH_WORD})\s+(\d{{1,2}})(?:st|nd|rd|th)?[,]?\s+(\d{{4}})\b', re.I)
_DAY_FIRST = re.compile(rf'\b(\d{{1,2}})\s+({_MONTH_WORD})\s+(\d{{4}})\b', re.I)


def _dates_in(quote):
    dates = {f'{int(y):04d}-{int(m):02d}-{int(d):02d}' for y, m, d in _DATE.findall(quote)}
    for month, day, year in _MONTH_FIRST.findall(quote):
        dates.add(f'{year}-{_MONTH_NUMBERS[month.lower()]:02d}-{int(day):02d}')
    for day, month, year in _DAY_FIRST.findall(quote):
        dates.add(f'{year}-{_MONTH_NUMBERS[month.lower()]:02d}-{int(day):02d}')
    return dates


def action_passage(title, excerpt, url, day, *, recovered=None):
    """Identify the strongest auditable wording in this report, never a verified action."""
    if recovered:
        return dict(kind='reconstructed_action_lead', basis='重建片段与官方行动线索匹配，待查原文',
                    quote=recovered['excerpt'], url=url)
    stage = None
    for field, value in (('标题', title), ('摘录', excerpt)):
        if field == '标题':
            value = _TITLE_SECTIONS.split(value, maxsplit=1)[0]
        for raw in re.split(r'(?<=[。！？.!?])\s*|\n+', value):
            quote = raw.strip()
            if not quote or len(quote) > 520 or not _REALIZED.search(quote) or _UNRESOLVED.search(quote):
                continue
            # Date and verb must be in the same sentence. Multiple different
            # dates or contextual uncertainty do not qualify as a dated signal.
            years = set(_YEAR.findall(quote))
            dates = _dates_in(quote) if years else set()
            if dates == {day} and years == {day[:4]}:
                return dict(kind='dated_action_report', basis=f'关联报道{field}同时出现当期日期和行动表述',
                            quote=quote, url=url)
            if not dates and not years and field == '标题' and stage is None:
                stage = dict(kind='stage_report', basis='关联报道标题有行动阶段表述，行动日期待核',
                             quote=quote, url=url)
    return stage
