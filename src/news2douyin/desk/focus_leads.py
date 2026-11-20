"""Route the shared candidate stream to focuses without creating event records."""
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import json
import time

from sqlalchemy import delete, func
from sqlmodel import Session, select

from ..dedup.normalize import canonicalize_url
from ..digest.calendar import BEIJING, day_bounds_utc
from ..digest.focus_hints import HeadlineFocusHints
from ..extraction.schema import stable_id
from ..intake.official_models import OfficialFocusUpdate
from ..selection.source_content import source_url
from ..storage.articles import lock_news
from . import intent, rules
from .focus_lifecycle import visible_watch
from .focus_models import LOCAL_METHOD, FocusDefinition, FocusLead
from .leases import acquire
from .models import DeskMeta, Watch

CURSOR = 'focus_lead_scan'
LOOKBACK = timedelta(days=1)
WINDOW = timedelta(minutes=30)
OVERLAP = timedelta(minutes=2)
WRITE_BATCH = 128
MODEL_PER_FOCUS_PER_DAY = 12
PRUNE_BATCH = 500


def _timestamp(value):
    return value.isoformat(timespec='seconds').replace('+00:00', 'Z')


def _observed(value):
    try:
        moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, TypeError, AttributeError):
        return None
    return moment.astimezone(timezone.utc) if moment.tzinfo is not None else None


def _scopes(session):
    return session.exec(select(Watch, FocusDefinition)
        .outerjoin(FocusDefinition, FocusDefinition.watch == Watch.key)
        .where(Watch.enabled.is_(True), visible_watch()).order_by(Watch.key))


class _Matcher:
    def __init__(self, scopes):
        self.scopes = []
        terms = defaultdict(int)
        for index, (watch, definition) in enumerate(scopes):
            spec = (intent.validate_spec(json.loads(definition.spec_json), description=definition.description)
                    if definition and definition.scope_version == watch.version else None)
            self.scopes.append((watch, spec, spec is not None and definition.method != LOCAL_METHOD))
            if spec is None:
                anchors = [alias for term in rules.terms(watch.scope) for alias in (term, *rules.ALIASES.get(term, ()))]
            else:
                anchors = [alias for entity in spec.entities for alias in intent._aliases(entity)]
                anchors.extend(spec.keywords)
                anchors.extend(term for relation in spec.relations for term in relation.keywords)
            for term in map(rules.norm, anchors):
                if term.strip():
                    terms[term.strip()] |= 1 << index
        self.trie = HeadlineFocusHints(
            [watch.key for watch, _ in scopes], {term: (mask, 0, 0) for term, mask in terms.items()},
            (1 << len(scopes)) - 1,
        )

    def matches(self, row):
        material = '\n'.join([row.excerpt, *row.actors])
        eligible = self.trie._matching_mask(row.title + '\n' + material)
        if not eligible:
            return
        card = SimpleNamespace(headings=rules.norm(row.title), material=rules.norm(material), sources='[]')
        while eligible:
            bit = eligible & -eligible
            eligible ^= bit
            watch, spec, use_model = self.scopes[bit.bit_length() - 1]
            if spec is not None:
                path = intent.recall(spec, card)
                if path:
                    yield watch, path, use_model
            else:
                reason = rules.match(watch, card)
                if reason:
                    yield watch, {'reason': reason}, False


def _store(engine, lease, leads, judge_day, model_budget):
    if not leads:
        return
    with Session(engine) as session:
        lock_news(session, 'lease/' + lease.key)
        lease.confirm(session)
        existing = set(session.exec(select(FocusLead.key).where(FocusLead.key.in_(leads))))
        for key, lead in leads.items():
            if key in existing:
                continue
            if lead.status == 'pending':
                if model_budget[lead.watch] < MODEL_PER_FOCUS_PER_DAY:
                    model_budget[lead.watch] += 1
                    lead.judge_day = judge_day
                else:
                    lead.status = 'done'
            session.add(lead)
        session.commit()
    leads.clear()


def _recalled(matcher, row):
    url = source_url(row.source_url)
    seen = _observed(row.observed_at)
    if row.source_kind == 'local' or not url or not seen:
        return
    ref = row.source_ref or {}
    provider = ref.get('provider') or row.source_kind
    evidence = row.excerpt and not row.reconstruction and ref.get('content_kind') != 'reconstruction'
    for watch, path, model in matcher.matches(row):
        key = 'lead_' + stable_id(watch.key, watch.version, canonicalize_url(url), row.title, row.excerpt)
        queued = model and bool(evidence) and row.source_kind != 'newsnow'
        yield FocusLead(key=key, watch=watch.key, scope_version=watch.version,
            candidate=row.key, observed_at=_timestamp(seen), title=row.title, url=url,
            provider=provider, language=row.language, excerpt=row.excerpt,
            recall_json=json.dumps(path, ensure_ascii=False), reason=path['reason'],
            status='pending' if queued else 'done')


def _expired_updates(cutoff):
    return select(OfficialFocusUpdate.event_id).where(
        OfficialFocusUpdate.available_at < _timestamp(cutoff - LOOKBACK)
    ).order_by(OfficialFocusUpdate.available_at).limit(PRUNE_BATCH)


def _prune(session, cutoff):
    expired = _expired_updates(cutoff)
    session.exec(delete(OfficialFocusUpdate).where(
        OfficialFocusUpdate.event_id.in_(expired),
        OfficialFocusUpdate.available_at < _timestamp(cutoff - LOOKBACK),
    ).execution_options(synchronize_session=False))


def scan_step(engine, storage_root, *, now=None, stop_event=None):
    """Advance one shared cursor. Rereads after restart or late projection are idempotent."""
    from ..candidates.service import iter_day

    instant = now or datetime.now(timezone.utc).replace(second=0, microsecond=0)
    if instant.tzinfo is None:
        raise ValueError('focus lead scan needs an aware cutoff')
    instant = instant.astimezone(timezone.utc)
    with Session(engine) as session:
        # The idle path needs no lease or write transaction.
        scopes = list(_scopes(session))
        state = session.get(DeskMeta, CURSOR)
        examined = json.loads(state.value) if state else {}
        should_prune = not scopes and session.exec(_expired_updates(instant)).first() is not None
    if not scopes:
        if should_prune:
            with Session(engine) as session:
                _prune(session, instant)
                session.commit()
        return False
    versions = [[watch.key, watch.version] for watch, _ in scopes]
    if examined.get('versions') == versions and examined.get('through', '') >= _timestamp(instant):
        return False

    with acquire(engine, 'focus-leads/scan') as lease:
        if lease is None:
            return False
        judge_day = instant.astimezone(BEIJING).date().isoformat()
        with Session(engine) as session:
            state = session.get(DeskMeta, CURSOR)
            saved = json.loads(state.value) if state else {}
            model_budget = defaultdict(int, dict(session.exec(
                select(FocusLead.watch, func.count()).where(FocusLead.judge_day == judge_day)
                .group_by(FocusLead.watch)).all()))
        since = datetime.fromisoformat(saved['through'].replace('Z', '+00:00')) if saved.get('through') else instant - LOOKBACK
        if saved.get('versions') != versions:
            since = min(since, instant - LOOKBACK)
        if since >= instant:
            return False

        day = since.astimezone(BEIJING).date()
        boundary, closed = day_bounds_utc(day)
        until = min(instant, closed, since + WINDOW)
        first = since - OVERLAP
        windows = [(day, max(boundary, first), until)]
        if first < boundary:
            windows.insert(0, (first.astimezone(BEIJING).date(), first, boundary))
        matcher = _Matcher(scopes)
        leads = {}

        def checkpoint():
            if stop_event is not None and stop_event.is_set():
                raise InterruptedError('focus lead scan stopped')
            lease.touch()

        try:
            for scan_day, lower, upper in windows:
                with closing(iter_day(engine, storage_root, scan_day, since=lower, cutoff=upper,
                                      checkpoint=checkpoint, receipts=True)) as rows:
                    for row in rows:
                        for lead in _recalled(matcher, row):
                            leads.setdefault(lead.key, lead)
                            if len(leads) >= WRITE_BATCH:
                                _store(engine, lease, leads, judge_day, model_budget)
            checkpoint()
        except InterruptedError:
            return False

        _store(engine, lease, leads, judge_day, model_budget)
        with Session(engine) as session:
            lock_news(session, 'lease/' + lease.key)
            lease.confirm(session)
            _prune(session, until)
            session.merge(DeskMeta(key=CURSOR, value=json.dumps({'through': _timestamp(until), 'versions': versions})))
            session.commit()
        return True


def _current_spec(session, lead):
    current = session.exec(select(Watch).where(
        Watch.key == lead.watch, Watch.version == lead.scope_version, Watch.enabled.is_(True), visible_watch()
    )).first()
    definition = session.get(FocusDefinition, lead.watch) if current else None
    if definition and definition.method != LOCAL_METHOD and definition.scope_version == lead.scope_version:
        return intent.validate_spec(json.loads(definition.spec_json), description=definition.description)


def judge_step(engine):
    """Judge relevance from one pinned excerpt outside database transactions."""
    with Session(engine) as session:
        keys = tuple(session.exec(select(FocusLead.key).where(
            FocusLead.status.in_(('pending', 'running')), FocusLead.next_attempt <= time.time()
        ).order_by(FocusLead.observed_at.desc(), FocusLead.key).limit(8)))
    for key in keys:
        with acquire(engine, 'focus-leads/judge/' + key) as lease:
            if lease is None:
                continue
            with Session(engine, expire_on_commit=False) as session:
                lead = session.get(FocusLead, key)
                if lead is None or lead.status not in ('pending', 'running'):
                    continue
                spec = _current_spec(session, lead)
                if spec is None:
                    lead.status = 'done'
                    session.add(lead)
                    session.commit()
                    continue
                lead.attempts += 1
                lead.status = 'running'
                session.add(lead)
                session.commit()

            # Revision one identifies this immutable excerpt snapshot, not an
            # ArticleVersion or a verified event. All citations use the lead id.
            source = dict(key=lead.key, revision=1, title=lead.title, content=lead.excerpt)
            result = intent.judge(spec, {'sources': [source]}, json.loads(lead.recall_json))
            with Session(engine) as session:
                lock_news(session, 'lease/' + lease.key, 'watch:' + lead.watch)
                lease.confirm(session)
                current = session.get(FocusLead, key)
                if current is None or _current_spec(session, current) is None:
                    return True
                current.decision = result['decision']
                current.reason = result['reason']
                current.evidence_json = json.dumps(result['evidence'], ensure_ascii=False)
                unavailable = result['method'] in ('unavailable', 'invalid')
                if unavailable and current.attempts < 3:
                    current.status = 'pending'
                    current.next_attempt = time.time() + 30 * 2 ** current.attempts
                else:
                    current.status = 'done'
                session.add(current)
                session.commit()
            return True
    return False


def recent(session, watch, *, offset=0, limit=12):
    rows = session.exec(select(FocusLead).where(
        FocusLead.watch == watch.key, FocusLead.scope_version == watch.version, FocusLead.decision != 'exclude'
    ).order_by(FocusLead.observed_at.desc(), FocusLead.key).offset(offset).limit(limit + 1)).all()
    return rows[:limit], len(rows) > limit
