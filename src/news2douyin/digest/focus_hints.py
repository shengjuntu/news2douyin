"""Read-only literal hints for unreviewed source headlines. No WatchEvent writes."""
import json
import re
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime

from .calendar import day_bounds_utc

from sqlmodel import Session, select

from ..desk.focus_lifecycle import visible_watch
from ..desk.focus_models import FocusBackfill, FocusDefinition
from ..desk.models import Watch
from ..desk.rules import ALIASES, norm, terms

MAX_HINTS = 8
GLOBAL_KINDS = frozenset({'gkg', 'gdelt', 'newsnow'})


def _known_before(value, cutoff):
    try:
        dt = datetime.fromisoformat((value or '').replace('Z', '+00:00'))
        return dt.tzinfo is not None and dt < cutoff
    except (TypeError, ValueError):
        return False


def _phrases(value, extra):
    phrases = set()
    for term in terms(value or ''):
        phrases.update(norm(t) for t in (term, *ALIASES.get(term, ()), *extra.get(term, ())) if t)
    return phrases


def _entity_aliases(encoded):
    if not encoded:
        return {}
    try:
        entities = json.loads(encoded)['entities']
        return {norm(e['name']): [a for a in e.get('aliases', ()) if isinstance(a, str)]
                for e in entities if isinstance(e.get('name'), str)}
    except (KeyError, TypeError, AttributeError, ValueError):
        return {}


def _word(char):
    return char.isascii() and char.isalnum()


class HeadlineFocusHints:
    """Match overlapping terms using a shared prefix trie and per-term watch masks."""
    def __init__(self, names, terms_to_masks, dimensionless, watch_keys=None):
        self.names = names
        self.watch_keys = watch_keys or {}
        self.dimensionless = dimensionless
        self.trie = {}
        for term, masks in terms_to_masks.items():
            node = self.trie
            for char in term:
                node = node.setdefault(char, {})
            node[None] = (term.isascii(), *masks)
        starts = ''.join(sorted(self.trie))
        self.starts = re.compile('[' + re.escape(starts) + ']') if starts else None

    def __bool__(self):
        return bool(self.names)

    def _matching_mask(self, headline, matched=None):
        if not self.starts:
            return 0
        text = norm(headline or '')
        anchors = dimensions = excluded = 0
        for start in self.starts.finditer(text):
            pos = start.start()
            previous_word = pos > 0 and _word(text[pos-1])
            node = self.trie
            while pos < len(text):
                node = node.get(text[pos])
                if node is None:
                    break
                pos += 1
                if None in node:
                    ascii_only, a, d, x = node[None]
                    if ascii_only and (previous_word or (pos < len(text) and _word(text[pos]))):
                        continue
                    if matched is not None and (a or d):
                        matched.append((text[start.start():pos], a | d))
                    anchors |= a
                    dimensions |= d
                    excluded |= x
        return anchors & (self.dimensionless | dimensions) & ~excluded

    def explain(self, headline, names):
        """Only re-inspect the saved, selected headlines for the words behind a hint."""
        matched = []
        eligible = self._matching_mask(headline, matched)
        details = {}
        while eligible and len(details) < MAX_HINTS:
            bit = eligible & -eligible
            name = self.names[bit.bit_length()-1]
            if name in names:
                words = list(dict.fromkeys(term for term, mask in matched if mask & bit))
                details[name] = dict(watch_key=self.watch_keys.get(name, ''), terms=words[:6])
            eligible ^= bit
        return details

    def __call__(self, headline):
        eligible = self._matching_mask(headline)
        names = []
        while eligible and len(names) < MAX_HINTS:
            bit = eligible & -eligible
            names.append(self.names[bit.bit_length()-1])
            eligible ^= bit
        return tuple(names)


def read_focus_hints(session: Session, day: date, *, cutoff=None):
    """Compile the active scope once; skip versions whose availability cannot be dated."""
    _, end = day_bounds_utc(day)
    if cutoff is not None:
        end = min(end, cutoff)
    query = (select(Watch.key, Watch.name, Watch.scope, Watch.dimensions, Watch.exclude, Watch.created, Watch.version,
                    FocusDefinition.scope_version, FocusDefinition.updated, FocusDefinition.spec_json,
                    FocusBackfill.scope_version, FocusBackfill.updated)
             .outerjoin(FocusDefinition, FocusDefinition.watch == Watch.key)
             .outerjoin(FocusBackfill, FocusBackfill.watch == Watch.key)
             .where(Watch.enabled.is_(True), visible_watch())
             .order_by(Watch.name, Watch.key))
    names = []
    watch_keys = {}
    postings = defaultdict(lambda: [0, 0, 0])
    dimensionless = 0
    for key, name, scope, dimensions, exclude, created, version, spec_v, changed, spec, job_v, job_at in session.exec(query):
        if not _known_before(created, end):
            continue
        if spec_v is not None:
            if spec_v != version or not _known_before(changed, end):
                continue
        elif version > 1 and (job_v != version or not _known_before(job_at, end)):
            continue
        extra = _entity_aliases(spec)
        anchors = _phrases(scope, extra)
        if not anchors:
            continue
        bit = 1 << len(names)
        names.append(name)
        watch_keys[name] = key
        required = _phrases(dimensions, extra)
        if not required:
            dimensionless |= bit
        for i, values in enumerate((anchors, required, _phrases(exclude, extra))):
            for term in values:
                postings[term][i] |= bit
    return HeadlineFocusHints(names, postings, dimensionless, watch_keys)


def annotate_focus(candidates, matcher, *, stats=None):
    """Unreviewed global sources can carry weak hints without changing membership."""
    if stats is not None:
        stats.setdefault('literal_focus_hints', 0)
    if not matcher:
        yield from candidates
        return
    for row in candidates:
        if row.source_kind in GLOBAL_KINDS and not row.watches:
            hints = matcher(row.title)
            if hints:
                row = replace(row, focus_hints=hints)
                if stats is not None:
                    stats['literal_focus_hints'] += 1
        yield row
