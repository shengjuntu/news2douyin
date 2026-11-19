"""Direct, within-edition matching of conservatively comparable action headlines.

The index holds only admitted representatives. A rejected variant never becomes a
new anchor: two different developments cannot merge through an intermediate title.
"""
from collections import defaultdict
import re
import unicodedata

# Completed/present-tense news headline variants. Different policy stages and
# opposite directions deliberately have distinct keys. Unsupported languages
# and paraphrases retain their baseline seats.
_FORMS = {
    'approve': ('approve', 'approves', 'approved'),
    'ratify': ('ratify', 'ratifies', 'ratified'),
    'sign': ('sign', 'signs', 'signed'),
    'reject': ('reject', 'rejects', 'rejected'),
    'suspend': ('suspend', 'suspends', 'suspended'),
    'resume': ('resume', 'resumes', 'resumed'),
    'lift': ('lift', 'lifts', 'lifted'),
    'impose': ('impose', 'imposes', 'imposed'),
    'reopen': ('reopen', 'reopens', 'reopened'),
    'close': ('close', 'closes', 'closed'),
    'recall': ('recall', 'recalls', 'recalled'),
    'evacuate': ('evacuate', 'evacuates', 'evacuated'),
    'arrest': ('arrest', 'arrests', 'arrested'),
    'release': ('release', 'releases', 'released'),
    'launch': ('launch', 'launches', 'launched'),
    'order': ('orders', 'ordered'),
    'catch': ('catch', 'catches', 'caught'),
    'capture': ('capture', 'captures', 'captured'),
}
_ACTION = {form: lemma for lemma, forms in _FORMS.items() for form in forms}
_PAST = {forms[-1] for forms in _FORMS.values()}
_ARTICLE = {'the', 'a', 'an'}
# Keep currency, signs, punctuation internal to quantities/names, questions and
# quotation marks. For instance, US$5m, €5m and a cut of -5% are different claims.
_WORD = re.compile(r"[^\W_]+(?:[.'/-][^\W_]+)*|[^\w\s]", re.UNICODE)
_CREDIT = re.compile(r'\s+(?:[-–—|]\s*(?:Reuters|Associated Press|AP News|AFP|新华社|路透社))\s*$', re.I)
_UNCERTAIN = re.compile(
    r"[?？“”\";:]|\b(?:could|may|might|will|would|plans?\s+(?:to|on)|planned|seeks?|hopes?|expected|"
    r"proposed|reportedly|allegedly|claims?|denies|denied|not|never|no|if|whether|"
    r"should|anniversary|recalls? when|yesterday|tomorrow)\b|n['’]t\b", re.I)
# Exact fine-grained roles are mandatory. A generic title ("Police arrest man")
# is too easily re-used across jurisdictions.
_GENERIC = {'police','government','minister','officials','man','woman','council','parliament',
            'city','president','court','senate','board','company','agency','commission'}
_NAME = r"[A-Z][A-Za-z0-9.-]*(?:[ \t]+[A-Z][A-Za-z0-9.-]*){0,3}"
# Apply an apparatus-based reading only when it is licensed by a whole
# grammatical clause. Bare surnames, a shared operator, or a shared device
# are insufficient. These reusable frames cover multiple physical actions.
_APPARATUS = {
    'catch': {'arm','arms','gripper','grippers','net'},
    'hoist': {'crane','cranes','winch','hoist','sling'},
    'inspect': {'scanner','scanners','sensor','sensors','probe'},
    'transport': {'shuttle','cart','conveyor','transporter'},
}
_EQUIPMENT_VERBS = {
    'catches':'catch','caught':'catch','captured':'catch',
    'hoists':'hoist','hoisted':'hoist','lifts':'hoist','lifted':'hoist',
    'inspects':'inspect','inspected':'inspect',
    'transports':'transport','transported':'transport',
}
_EQUIPMENT_MODIFIERS = {'a','an','the','giant','mechanical','robotic','automated','hydraulic',
                        'tower','launch','pad','shipboard','deck','dockside','gantry',
                        'overhead','cargo','mobile','remote','optical','thermal','inspection'}
_MOTION = r'orbit|sea|flight|port'
_OPERATOR = re.compile(
    rf"(?P<owner>{_NAME}) (?:(?i:successfully) )?(?P<verb>[a-z]+) (?P<asset>{_NAME})"
    rf"(?: (?i:after|on return from) (?P<motion>{_MOTION}))?")
_TOOL = re.compile(
    rf"(?P<owner>{_NAME})'s (?P<asset>{_NAME}) "
    rf"(?:(?i:returns? from) (?P<motion>{_MOTION}) (?i:and) )?"
    rf"(?i:is |was )?(?P<verb>[a-z]+) by (?P<tool>[a-z ]+)")
# Remove a few patent editorial flourishes *only* in the apparatus grammar.
_EDITORIAL = re.compile(r' (?:and (?:makes|made) history|in (?:a )?(?:dramatic |historic )first)$', re.I)
_APPROVAL = re.compile(r'\b(?:gave (?:its |their )?approval to|signed off on|'
                       r'gave (?:the )?green light to)\b', re.I)
_GRANTED = re.compile(r'\bgranted approval by\b', re.I)
_DOCUMENT = re.compile(r'\b(?:accord|agreement|contract|pact|treaty)\b', re.I)
_INK = re.compile(r'\b(?:inks|inked)\b', re.I)


# A public order is often reported twice with the same motivating incident,
# once in the passive voice. Compare the cause and named facility location
# before treating omission of a generic enforcement agent as a paraphrase.
_ORDER_TYPE = r'(?:evacuation|shelter-in-place|boil-water|curfew)(?: orders?| advisories| advisory)?'
_ORDER_AGENT = r'(?:local police|police|authorities|officials|emergency officials)'
_ORDER_ACTION = re.compile(
    rf'(?:(?P<agent>{_ORDER_AGENT}) (?:lift|lifts|lifted) (?:the )?(?P<active>{_ORDER_TYPE})'
    rf'|(?:the )?(?P<passive>{_ORDER_TYPE}) (?:is |was )?lifted)', re.I)
_CONTEXT = re.compile(r'(?P<cause>[\w -]+) (?P<where>near|at|outside|inside) (?:the )?(?P<site>.+)', re.I)
_FACILITY = r'[a-z][a-z-]*(?: [a-z][a-z-]*){0,2} (?:plant|refinery|terminal|depot|factory|station|hospital|market)'
_DIRECTION = r'(?:(?P<direction>central|northern|southern|eastern|western) )?'
_SITE_THEN_PLACE = re.compile(rf'(?P<facility>{_FACILITY}) in {_DIRECTION}(?P<place>{_NAME})')
_PLACE_THEN_SITE = re.compile(rf'{_DIRECTION}(?P<place>{_NAME}) (?P<facility>{_FACILITY})')
_CAUSE_VERB = {'found','discovered','detected','contained','reported','extinguished','leaked','exploded'}


def _public_order(title):
    action, after, incident = title.partition(' after ')
    if not after or not (order := _ORDER_ACTION.fullmatch(action)):
        return None
    context = _CONTEXT.fullmatch(incident)
    if not context:
        return None
    site = (_SITE_THEN_PLACE.fullmatch(context['site']) or
            _PLACE_THEN_SITE.fullmatch(context['site']))
    if not site:
        return None
    cause = tuple(w for w in context['cause'].casefold().split() if w not in _ARTICLE)
    if not 2 <= len(cause) <= 8 or not _CAUSE_VERB.intersection(cause):
        return None
    kind = (order['active'] or order['passive']).casefold().removesuffix(' orders').removesuffix(' order')
    frame = ('public-order', kind, cause, context['where'].casefold(),
             site['facility'].casefold(), site['place'].casefold())
    return frame, order['agent'], site['direction']


def _instrument_frame(title):
    simple = _EDITORIAL.sub('', title)
    match = _TOOL.fullmatch(simple) or _OPERATOR.fullmatch(simple)
    if not match or not (verb := _EQUIPMENT_VERBS.get(match['verb'])):
        return None
    if tool := match.groupdict().get('tool'):
        words = tool.split()
        if words[-1] not in _APPARATUS[verb] or not _EQUIPMENT_MODIFIERS.issuperset(words[:-1]):
            return None
    owner, asset = match['owner'].casefold(), match['asset'].casefold()
    if (owner == asset or owner in _GENERIC or asset in _GENERIC or
            min(len(owner), len(asset)) < 3):
        return None
    return ('apparatus', verb, owner, asset)


def _word_frames(title):
    title = _APPROVAL.sub('approved', _GRANTED.sub('approved by', title))
    if _DOCUMENT.search(title):
        title = _INK.sub('signed', title)
    words = [w.casefold() for w in _WORD.findall(title) if w.casefold() not in _ARTICLE]
    actions = [(i, _ACTION[word]) for i, word in enumerate(words) if word in _ACTION]
    if len(actions) != 1:
        return []
    pos, verb = actions[0]
    if not pos or pos == len(words) - 1 or len(words) < 4:
        return []
    # Require a concrete named anchor beyond a generic first word/office.
    proper = {w.casefold() for w in _WORD.findall(title) if w[0].isupper()}
    if not proper.difference(_GENERIC, _ARTICLE) or not any(len(w) >= 4 for w in words):
        return []
    before, after = words[:pos], words[pos + 1:]
    if words[pos] in _PAST and before[-1] in {'is','are','was','were'} and after[0] == 'by':
        subject, target = after[1:], before[:-1]
    else:
        if words[pos] in _PAST and before[-1] in {'has','have'}:
            before = before[:-1]
        subject, target = before, after
    if not subject or not target:
        return []
    # Consider word order, participants, all adjuncts, numbers, places, and
    # modifiers. Only the licensed verbal variants and articles disappear.
    return [('role', verb, tuple(subject), tuple(target))]


def _title(card):
    title = ' '.join(unicodedata.normalize('NFKC', str(card.get('title') or '')).split())
    return _CREDIT.sub('', title).strip().rstrip('.').replace('’', "'")


def _frames(card):
    title = _title(card)
    if not title or len(title) > 500 or card.get('reconstruction') or _UNCERTAIN.search(title):
        return ()
    frames = _word_frames(title)
    if frame := _instrument_frame(title):
        frames.append(frame)
    if order := _public_order(title):
        frames.append(order[0])
    return frames


def _conflicts(first, second):
    def country(card):
        geo = card.get('geography') or {}
        if geo.get('basis') == 'official_action':
            return geo.get('primary')
        if card.get('source_kind') == 'gdelt':
            return card.get('country')
        return None

    def actors(card):
        if card.get('source_kind') == 'gdelt':
            return tuple(str(name).casefold() for name in (card.get('actors') or ()))
        return None

    def explicit_date(card):
        timing = card.get('source_clock') or {}
        return timing.get('event_day') or timing.get('event_date')

    def movement(card):
        m = re.search(rf'\b(?:after|from) ({_MOTION})\b', card.get('title') or '', re.I)
        return m[1].casefold() if m else None

    order_a = _public_order(_title(first))
    order_b = _public_order(_title(second))
    if order_a and order_b:
        for a, b in zip(order_a[1:], order_b[1:]):
            if a and b and a.casefold() != b.casefold():
                return True
    for a, b in ((country(first), country(second)),
                 (movement(first), movement(second)),
                 (explicit_date(first), explicit_date(second)), (actors(first), actors(second)),
                 ((first.get('official_coverage') or {}).get('event_id'),
                  (second.get('official_coverage') or {}).get('event_id'))):
        if a and b and a != b:
            return True
    return False


class ActionSeatIndex:
    """Indexes comparable title frames among this shortlist's accepted seats."""
    def __init__(self):
        self._by_frame = defaultdict(list)
        self._keys = set()

    def add(self, card):
        key = card['key']
        if key in self._keys:
            return
        self._keys.add(key)
        for frame in _frames(card):
            self._by_frame[frame].append(card)

    def find(self, card):
        matches = {}
        for frame in _frames(card):
            for representative in self._by_frame.get(frame, ()):
                if not _conflicts(representative, card):
                    matches[representative['key']] = frame[0]
        if len(matches) != 1:
            return None
        key, reason = next(iter(matches.items()))
        reasons = {
            'role': '标题中的施事、行动、受事与限定信息一致',
            'apparatus': '标题中的具名机构、设备对象与机械操作一致',
            'public-order': '解除的公共限制、触发事件、设施及具名地点一致',
        }
        return key, reasons[reason]
