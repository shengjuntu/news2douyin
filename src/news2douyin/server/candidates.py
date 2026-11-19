"""Candidate browsing and the explicit handoff into the local event library."""
from uuid import uuid4
from urllib.parse import quote, urlencode

from fastapi import Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select

from ..search.service import event_query
from ..storage.models import Event, EventState
from ..translations.language import describe_language
from .research import same_origin
from .tasks import task_call


class OrganizeCandidate(BaseModel):
    model_config = ConfigDict(extra='forbid')

    candidate_key: str = Field(min_length=1, max_length=256)
    expected_token: str = Field(pattern=r'^[a-f0-9]{64}$')
    request_id: str = Field(min_length=8, max_length=128)
    event_key: str = Field(default='', max_length=160)
    expected_version: int | None = Field(default=None, ge=0)
    title: str = Field(default='', max_length=250)
    note: str = Field(default='', max_length=1000)


_LIST_LIMIT = 30


def _targets(session, query):
    results = session.exec(event_query(session, query=query).order_by(
        Event.last_seen_at.desc(), Event.event_key).limit(30)).all()
    keys = [event.event_key for event in results]
    versions = dict(session.exec(select(EventState.event_key, EventState.version).where(
        EventState.event_key.in_(keys))).all()) if keys else {}
    return [dict(event_key=event.event_key, title=event.event_title, version=versions.get(event.event_key, 0),
                 last_seen_at=event.last_seen_at) for event in results]


def register(app, engine, storage_root, render):
    @app.get('/candidates', response_class=HTMLResponse, include_in_schema=False)
    def reports(q: str = Query('', max_length=200), language: str = Query('', max_length=40),
                provider: str = Query('', max_length=80), before_at: str = Query('', max_length=40),
                before_key: str = Query('', max_length=256)):
        from ..candidates.service import list_reports
        from ..translations.language import normalize_language

        q, provider, language = q.strip(), provider.strip(), language.strip()
        code = normalize_language(language, provider=provider)
        if language and not code:
            raise HTTPException(422, '请输入语言代码，例如 ar 或 zh-Hant')
        if bool(before_at) != bool(before_key):
            raise HTTPException(422, '分页位置不完整')
        before = (before_at, before_key) if before_at else None
        with Session(engine) as session:
            found = task_call(list_reports, session, query=q, language=code, provider=provider,
                              before=before, limit=_LIST_LIMIT + 1)
        rows = []
        for item in found[:_LIST_LIMIT]:
            source = item.source_ref or {}
            rows.append(dict(key=item.key, title=item.title, observed_at=item.observed_at,
                             language=describe_language(declared=item.language)['display'],
                             provider=source.get('provider') or item.source_kind))
        more_url = ''
        if len(found) > _LIST_LIMIT:
            last = rows[-1]
            more_url = '/candidates?' + urlencode(dict(q=q, language=language, provider=provider,
                                                        before_at=last['observed_at'], before_key=last['key']))
        return render('candidates.html', active='radar', rows=rows, q=q, language=language,
                      provider=provider, more_url=more_url)

    @app.get('/organize', response_class=HTMLResponse, include_in_schema=False)
    def organize_page(candidate: str = Query(min_length=1, max_length=256)):
        from ..candidates.service import candidate_token, resolve_material

        with Session(engine) as session:
            material = task_call(resolve_material, session, storage_root, candidate)
        language = describe_language(declared=material.get('language', ''), provider=material.get('provider', ''))
        return render('organize.html', active='radar', material=material, language=language,
                      candidate_key=candidate, expected_token=candidate_token(material['candidate']),
                      request_id=uuid4().hex)

    @app.get('/api/candidates/organize/targets')
    def targets(q: str = Query('', max_length=200)):
        with Session(engine) as session:
            return task_call(_targets, session, q.strip())

    @app.post('/api/candidates/organize', dependencies=[Depends(same_origin)])
    def organize(payload: OrganizeCandidate):
        from ..candidates.promotion import promote

        data = payload.model_dump()
        key = data.pop('candidate_key')
        with Session(engine) as session:
            result = task_call(promote, session, storage_root, key, **data)
        return dict(event_key=result['event_key'], version=result['version'],
                    url='/events/' + quote(result['event_key'], safe=''))
