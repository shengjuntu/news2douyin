"""A bounded, replayable evidence view of selected GDELT source records.

OfficialMention is one row per (GlobalEventID, exact document identifier).
The edge preserves the first/last raw references. Neither mention rows nor
document metadata establish that an article's other subjects took part.
"""
from __future__ import annotations

from hashlib import sha256
import json
from uuid import uuid4

from sqlalchemy import delete, update
from sqlmodel import Session, select

from ..intake.official_models import OfficialEvent, OfficialMention
from ..storage.articles import identity_key, lock_news
from ..storage.models import Article, ArticleIdentity
from ..storage.sql import json_text
from .models import GkgSource, GraphPending, OfficialGraphDocument
from .service import enqueue_graph

DOCUMENT_LIMIT = 32
SUBJECT_LIMIT = 12
_OWNER = 'official_gdelt_evidence'
_GRAPH = 'active_global'


def status(session, event_id):
    row = session.get(GraphPending, 'official/'+event_id)
    state = 'not_requested' if row is None else 'ready' if row.written == row.token else 'pending'
    return dict(event_id=event_id, graph=_GRAPH, node_key='gdelt/'+event_id, state=state,
                document_limit=DOCUMENT_LIMIT)


def track(session, event_id):
    """Enroll this selected key. Missing/removed archive records can be skipped."""
    if not event_id:
        return False
    # Serialize enrollment with the intake transaction's final invalidation.
    # There is never a graph/network request under this short write lock.
    lock_news(session,'intake:projection-turn')
    if session.get(OfficialEvent, event_id) is None:
        return False
    enqueue_graph(session, 'official/'+event_id)
    return True


def request(session, event_id):
    if not track(session, event_id):
        raise KeyError('没有找到这条 GDELT 官方事件')
    return dict(event_id=event_id, state='queued', graph=_GRAPH, node_key='gdelt/'+event_id,
                document_limit=DOCUMENT_LIMIT)


def refresh_tracked(session, event_ids):
    """One indexed update for this committed batch. Never enroll unselected IDs."""
    keys = ['official/'+event_id for event_id in sorted(set(event_ids))]
    if keys:
        session.exec(update(GraphPending).where(GraphPending.key.in_(keys)).values(token=uuid4().hex)
                     .execution_options(synchronize_session=False, n2d_query_name='official.graph.invalidate'))


def _key(*values):
    return sha256(json.dumps(values,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def refresh_documents(session, identifiers):
    hashes = [_key(identifier) for identifier in sorted(set(identifiers))]
    if hashes:
        enrolled = select(OfficialGraphDocument.graph_key).where(OfficialGraphDocument.document_hash.in_(hashes))
        session.exec(update(GraphPending).where(GraphPending.key.in_(enrolled)).values(token=uuid4().hex)
                     .execution_options(synchronize_session=False, n2d_query_name='official.graph.gkg_arrived'))


def _watch_documents(session, event_id, documents):
    graph_key = 'official/'+event_id
    previous = set(session.exec(select(OfficialGraphDocument.document_hash)
                               .where(OfficialGraphDocument.graph_key == graph_key)))
    current = {_key(m.doc_identifier) for m in documents}
    if previous-current:
        session.exec(delete(OfficialGraphDocument).where(OfficialGraphDocument.graph_key == graph_key,
                     OfficialGraphDocument.document_hash.in_(previous-current)))
    session.add_all(OfficialGraphDocument(graph_key=graph_key,document_hash=key) for key in current-previous)


def _document_subjects(persons, organizations):
    # Name clusters belong to the document's GKG annotation. Role identity
    # stays in the official event record and is never inferred from these.
    subjects = {}
    for kind, encoded in (('person',persons), ('organization',organizations)):
        try:
            values = encoded if isinstance(encoded,list) else json.loads(encoded or '[]')
        except (TypeError, ValueError):
            continue
        if not isinstance(values, list):
            continue
        for name in values:
            if isinstance(name,str) and 0 < len(name.strip()) <= 500:
                name = name.strip()
                subject = _key(kind,name)
                subjects[subject] = [subject,kind,name]
                if len(subjects)>SUBJECT_LIMIT:
                    return list(subjects.values())[:SUBJECT_LIMIT], True
    return list(subjects.values()), False


def _gkg(session, documents):
    return gkg_for_identifiers(session, (m.doc_identifier for m in documents))


def gkg_for_identifiers(session, identifiers):
    """Resolve bounded indexed URL identities, then verify exact identifiers.

    Imported canonical identities may merge URLs. Legacy snapshots establish a
    link only when they still record the same GKG record ID and original URI.
    Unresolved identifiers remain fully valid GDELT Mentions evidence.
    """
    wanted = {url for url in identifiers if url.startswith(('https://','http://'))}
    if not wanted:
        return {}
    identities = set()
    for url in wanted:
        try:
            identities.add(identity_key({'url':url}))
        except ValueError:
            # A malformed URL is still a source document identifier.
            continue
    if not identities:
        return {}
    query = (select(GkgSource.record_id, json_text(GkgSource.attributes, 'document_identifier'),
                    json_text(GkgSource.attributes, 'intake_feed'), json_text(GkgSource.attributes, 'intake_batch'),
                    json_text(GkgSource.attributes, 'intake_row'), json_text(GkgSource.attributes, 'persons'),
                    json_text(GkgSource.attributes, 'orgs'),
                    json_text(Article.raw_json, 'record_id'), json_text(Article.raw_json, 'uri'))
             .select_from(ArticleIdentity).join(Article, ArticleIdentity.article_key == Article.article_key)
             .join(GkgSource, Article.article_key == GkgSource.article_key)
             .where(ArticleIdentity.identity_key.in_(identities)))
    result = {}
    for record, identifier, feed, batch, row, persons, orgs, legacy_record, legacy_uri in session.exec(query):
        if not identifier and legacy_record == record:
            identifier = legacy_uri
        if identifier in wanted:
            subjects, more = _document_subjects(persons,orgs)
            result[identifier] = ([record,feed,batch,int(row) if str(row).isdigit() else None,more],subjects)
    return result


def _cleanup(q, key, stamp):
    # Walk only stale edges from this bounded root. Preserve target nodes used
    # by other official records, and every foreign/local edge.
    # Before removing the last event reference, release its document-level
    # subjects. A retry after any completed query can still discover the doc.
    q('MATCH (:SourceEvent {key:$key})-[r:MENTIONED_IN]->(d:GdeltDocument) '
      'WHERE r.evidence_owner=$owner AND r.evidence_stamp<>$stamp AND indegree(d)=1 '
      'WITH d MATCH (d)-[m:GKG_MENTIONS]->(n:GkgSubject) WHERE m.evidence_owner=$owner '
      'DELETE m WITH DISTINCT n WHERE NOT (n)--() DELETE n', key=key,owner=_OWNER,stamp=stamp)
    q('MATCH (e:SourceEvent {key:$key})-[r]->(n) '
      'WHERE r.evidence_owner=$owner AND r.evidence_stamp<>$stamp '
      'DELETE r WITH DISTINCT n WHERE NOT (n)--() DELETE n', key=key, owner=_OWNER, stamp=stamp)


def project(engine, mirror, event_id):
    with Session(engine,expire_on_commit=False) as session:
        # Register late GKG interest from the same source snapshot. An intake
        # commit racing with graph I/O invalidates the captured outbox token.
        lock_news(session,'intake:projection-turn')
        event = session.get(OfficialEvent,event_id)
        documents = session.exec(select(OfficialMention).where(OfficialMention.event_id == event_id)
            .order_by(OfficialMention.last_seen.desc(), OfficialMention.key.desc())
            .limit(DOCUMENT_LIMIT+1)).all() if event else []
        more = len(documents) > DOCUMENT_LIMIT
        documents = documents[:DOCUMENT_LIMIT]
        _watch_documents(session,event_id,documents)
        gkg = _gkg(session, documents)
        session.commit()
    key = 'gdelt/'+event_id
    stamp = uuid4().hex
    def q(query, **params):
        return mirror.query(_GRAPH, query, **params)
    if event is None:
        _cleanup(q, key, stamp)
        q('MATCH (e:SourceEvent {key:$key}) SET e.evidence_available=false '
          'WITH e WHERE NOT (e)--() DELETE e', key=key)
        return
    q('MERGE (e:SourceEvent {key:$key}) SET e.provider=$provider,e.external_id=$event_id,'
      'e.event_date=$day,e.event_code=$code,e.event_root_code=$root,e.actor1_name=$a1,e.actor2_name=$a2,'
      'e.action_country_iso2=$country,e.action_place=$place,e.source_url=$url,'
      'e.first_seen=$first,e.last_seen=$last,e.feed=$feed,e.stamp=$batch,e.row_index=$row,'
      'e.evidence_available=true,e.projected_documents=$count,e.has_more_documents=$more,'
      'e.document_limit=$limit', key=key, provider='gdelt', event_id=event_id, day=event.event_date,
      code=event.event_code, root=event.event_root_code, a1=event.actor1_name, a2=event.actor2_name,
      country=event.action_country_iso2, place=event.action_place, url=event.source_url,
      first=event.first_seen, last=event.last_seen, feed=event.feed, batch=event.stamp, row=event.row_index,
      count=len(documents), more=more, limit=DOCUMENT_LIMIT)
    # GdeltActor represents this coded name/country combination, distinct from
    # verified Person identity and from any person named in a GKG document.
    for edge, name, country, iso2 in (
        ('CODED_ACTOR1',event.actor1_name,event.actor1_country,event.actor1_country_iso2),
        ('CODED_ACTOR2',event.actor2_name,event.actor2_country,event.actor2_country_iso2),
    ):
        if name or country or iso2:
            q('MATCH (e:SourceEvent {key:$key}) MERGE (a:GdeltActor {key:$actor}) '
              'SET a.name=$name,a.country_code=$country,a.iso2=$iso '
              'MERGE (e)-[r:'+edge+']->(a) SET r.evidence_owner=$owner,r.evidence_stamp=$stamp',
              key=key, actor=_key(name,country,iso2), name=name, country=country, iso=iso2,
              owner=_OWNER, stamp=stamp)
    if event.action_place or event.action_country or event.action_country_iso2 or event.latitude is not None:
        q('MATCH (e:SourceEvent {key:$key}) MERGE (p:GdeltPlace {key:$place}) '
          'SET p.name=$name,p.country_code=$country,p.iso2=$iso,p.latitude=$lat,p.longitude=$lon '
          'MERGE (e)-[r:CODED_ACTION_AT]->(p) SET r.evidence_owner=$owner,r.evidence_stamp=$stamp',
          key=key,place=_key(event.action_place,event.action_country,event.action_country_iso2,
                             event.latitude,event.longitude),
          name=event.action_place,country=event.action_country,iso=event.action_country_iso2,
          lat=event.latitude,lon=event.longitude,owner=_OWNER,stamp=stamp)
    if documents:
        rows = []
        subjects = []
        document_keys = []
        for m in documents:
            document_key = _key(m.doc_identifier)
            metadata, names = gkg.get(m.doc_identifier, ([None,None,None,None,None],[]))
            document_keys.append(document_key)
            subjects.extend([document_key,*name] for name in names)
            rows.append([document_key, m.doc_identifier, m.key, m.first_seen, m.last_seen,
                         m.first_feed, m.first_stamp, m.first_row_index, m.last_feed, m.last_stamp,
                         m.last_row_index, m.confidence, m.sentence_id, m.actor1_offset,m.actor2_offset,
                         m.action_offset, m.domain, m.source_name,
                         *metadata])
        q('MATCH (e:SourceEvent {key:$key}) UNWIND $rows AS x '
          'MERGE (d:GdeltDocument {key:x[0]}) SET d.identifier=x[1],d.gkg_record_id=x[18],'
          'd.gkg_feed=x[19],d.gkg_stamp=x[20],d.gkg_row=x[21],d.has_more_subjects=x[22],d.subject_limit=$subject_limit '
          'MERGE (e)-[r:MENTIONED_IN]->(d) SET r.mention_key=x[2],r.first_seen=x[3],r.last_seen=x[4],'
          'r.first_feed=x[5],r.first_stamp=x[6],r.first_row=x[7],r.last_feed=x[8],r.last_stamp=x[9],'
          'r.last_row=x[10],r.confidence=x[11],r.sentence_id=x[12],r.actor1_offset=x[13],'
          'r.actor2_offset=x[14],r.action_offset=x[15],r.domain=x[16],r.source_name=x[17],'
          'r.evidence_owner=$owner,r.evidence_stamp=$stamp', key=key,rows=rows,owner=_OWNER,stamp=stamp,subject_limit=SUBJECT_LIMIT)
        if subjects:
            q('UNWIND $rows AS x MATCH (d:GdeltDocument {key:x[0]}) MERGE (n:GkgSubject {key:x[1]}) '
              'SET n.kind=x[2],n.name=x[3] MERGE (d)-[m:GKG_MENTIONS]->(n) '
              'SET m.evidence_owner=$owner,m.evidence_stamp=$stamp',rows=subjects,owner=_OWNER,stamp=stamp)
        q('UNWIND $docs AS k MATCH (:GdeltDocument {key:k})-[m:GKG_MENTIONS]->(n:GkgSubject) '
          'WHERE m.evidence_owner=$owner AND m.evidence_stamp<>$stamp '
          'DELETE m WITH DISTINCT n WHERE NOT (n)--() DELETE n',docs=document_keys,owner=_OWNER,stamp=stamp)
    _cleanup(q,key,stamp)
