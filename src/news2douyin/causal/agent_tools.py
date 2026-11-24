"""Case-scoped, evidence-backed investigation tools for an existing research run."""
from datetime import date, datetime
import hashlib
import re
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from sqlmodel import Session, select

from ..dedup.normalize import canonicalize_url
from ..research import service as research
from ..research.models import ResearchSource
from ..selection.source_content import source_url
from ..storage.models import utc_now_iso
from ..storage.utils import dumps, loads
from . import service as causal
from .chronology import CHINESE_DATE, LITERAL, eligible, excerpt_time, interval, sqlite_utc_bound
from .models import (CausalAgentLink, CausalAgentNote, CausalAgentQuery, CausalJob, CausalNode,
                     CausalProgramRetrieval, CausalProgramStep)
from .retrieval import retrieve


MAX_SEARCHES = 12
SEARCHES_PER_PARENT = 3
MAX_RESULTS = 12
_LOCAL_EXCERPT_KINDS = {'local_article', 'publisher_preview', 'reviewed_moment'}
_PROGRAM_EXCERPT_KINDS = _LOCAL_EXCERPT_KINDS | {'publisher_history'}
Key = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
Day = Annotated[str, StringConstraints(pattern=r'^\d{4}-\d{2}-\d{2}$')]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Relation = Literal['possible_cause', 'context', 'contradicts', 'document_reference']
_MONTH = r'(?:January|February|March|April|May|June|July|August|September|October|November|December)'
_ENGLISH_DATE = re.compile(r'\b(?:' + _MONTH + r'\s+\d{1,2},\s*\d{4}|\d{1,2}\s+' +
                           _MONTH + r'\s+\d{4})\b', re.IGNORECASE)


class AgentArgs(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    run_id: Key


class SearchArgs(AgentArgs):
    node_key: Key
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    date_from: Day
    date_to: Day


class SavedCandidate(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    search_id: Key
    ref: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
    reason: Reason
    relation: Relation


class ExternalCandidate(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    source_id: Key
    paragraph_id: Key
    quote: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1200)]
    occurred_at: Annotated[str, StringConstraints(strip_whitespace=True, max_length=48)] = ''
    reason: Reason
    relation: Relation


class SaveArgs(AgentArgs):
    node_key: Key
    candidates: list[SavedCandidate | ExternalCandidate] = Field(default_factory=list, max_length=causal.RESULTS)
    stop_reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] = ''


_TOOLS = {
    'causal_get_investigation': (AgentArgs, '读取本次调查的事件树、日期范围和执行上限。先读取，再选父节点检索。'),
    'causal_search_events': (SearchArgs, '按显式起止日检索父节点的本地事件与报道。日期包含两端；与调查及父节点时间取交集。保存返回的 search_id 和 ref。'),
    'causal_save_step': (SaveArgs, '保存本次父节点的备选前因、背景、反证或文献引用，或记录停止原因。只提交本节点已保存的 search_id/ref，或本课题已读取的 source_id/paragraph_id 及其中实际存在的 quote/text。发生日期须出现在引文中。返回的 evidence 可用于 research_save_claim。')
}
NAMES = frozenset(_TOOLS)


def tool_list():
    return [dict(name=name, description=description, inputSchema=model.model_json_schema())
            for name, (model, description) in _TOOLS.items()]


def _scope(session, run_id, *, timed=False):
    research_case, run = research.active_run(session, run_id, enforce_time=timed)
    link = session.exec(select(CausalAgentLink).where(
        CausalAgentLink.research_case_id == research_case.case_id)).first()
    if not link or link.current_run_id != run_id:
        raise ValueError('该研究执行没有绑定当前前因调查')
    return link, causal._require(session, link.case_key), research_case, run


def _node(session, case_key, key):
    row = session.get(CausalNode, key)
    if row is None or row.case_key != case_key:
        raise ValueError('该节点不属于当前调查')
    return row


def _bounds(session, case, node, research_case, run):
    # Match the local investigation's occurrence-first rule. An earlier dated
    # ancestor also limits the effective upper bound of any descendant.
    as_of = min(research.timestamp(case.created_at), research.timestamp(run.cutoff or research_case.cutoff))
    anchor, excluded, visited = None, set(), set()
    current = node
    while current is not None:
        if current.key in visited or len(visited) > causal.MAX_DEPTH:
            raise ValueError('调查节点链无效')
        visited.add(current.key)
        excluded.update(value for value in (current.ref, current.event_key) if value)
        point = interval(current.occurred_at) or interval(current.published_at)
        if point is not None:
            anchor = anchor or point
            historical = _historical_anchor(current)
            as_of = min(as_of, historical[0] if historical else point[1])
        current = _node(session, case.key, current.parent_key) if current.parent_key else None
    return anchor, as_of, excluded


def _search_bounds(args, floor, as_of):
    first, last = interval(args.date_from), interval(args.date_to)
    beginning = interval(floor)
    if first is None or last is None or beginning is None or first[0] > last[0]:
        raise ValueError('请选择有效的检索起止日')
    lower, upper = max(first[0], beginning[0]), min(last[1], as_of)
    if lower >= upper:
        raise ValueError('所选日期与本节点可检索时间没有交集')
    return lower, upper


def _historical_anchor(node):
    if node.time_status == 'unknown' and any(
        source.get('retrospective') for source in loads(node.sources_json, [])
    ):
        return interval(node.occurred_at)
    return None


def _expandable(node, note):
    historical = note and note.relation in {'context', 'document_reference'} and _historical_anchor(node)
    return node.depth < causal.MAX_DEPTH and (node.depth == 0 or node.time_status == 'earlier' or bool(historical))


def _check_branch(session, node, run):
    note = session.get(CausalAgentNote, node.key) if node.time_status == 'unknown' else None
    if not _expandable(node, note):
        raise ValueError('该节点未确认早于上层，或已达到调查深度上限')
    used = set(session.exec(select(CausalNode.parent_key).where(
        CausalNode.case_key == node.case_key, CausalNode.depth == node.depth + 1).distinct()))
    used.update(session.exec(select(CausalAgentQuery.node_key).join(
        CausalNode, CausalNode.key == CausalAgentQuery.node_key
    ).where(CausalAgentQuery.case_key == node.case_key, CausalAgentQuery.run_id == run.run_id,
            CausalNode.depth == node.depth).distinct()))
    if node.key not in used and len(used) >= causal.BEAM_WIDTH:
        raise ValueError('本层展开节点数已达上限，请查看已有线索')


def _earlier_than(point, lower):
    return point is not None and point[0] < lower and point[1] <= lower


def _in_scope(candidate, lower, upper, anchor):
    if _earlier_than(interval(candidate.get('occurred_at', '')), lower):
        return None
    sources = [source for source in candidate.get('sources', [])
               if source.get('quote') and not _earlier_than(interval(source.get('published_at', '')), lower)]
    return eligible({**candidate, 'sources': sources}, anchor, upper)


def _get(engine, args):
    from ..research.causal_dispatch import analysis_only, ANALYSIS_TOOLS
    with Session(engine) as session:
        link, case, research_case, run = _scope(session, args.run_id)
        analysis = analysis_only(session, run.run_id)
        # Already-saved sources are bounded by the research case cap. No source
        # is registered, fetched or modified by this read-only tree tool.
        source_index = {row.source_id: row for row in session.exec(select(ResearchSource).where(
            ResearchSource.case_id == research_case.case_id))} if analysis else {}
        nodes = []
        rows = list(session.exec(select(CausalNode, CausalAgentNote, CausalProgramStep, CausalJob,
                                        CausalProgramRetrieval).outerjoin(
            CausalAgentNote, CausalAgentNote.node_key == CausalNode.key
        ).outerjoin(CausalProgramStep, CausalProgramStep.node_key == CausalNode.key
        ).outerjoin(CausalJob, CausalJob.node_key == CausalNode.key
        ).outerjoin(CausalProgramRetrieval, CausalProgramRetrieval.node_key == CausalNode.key
        ).where(CausalNode.case_key == case.key).order_by(CausalNode.depth, CausalNode.key).limit(causal.MAX_TREE_NODES)))
        completed_steps = sum(bool(job and job.state in {'ready', 'failed'}) for _, _, _, job, _ in rows)
        for node, note, step, job, report in rows:
            sources = loads(node.sources_json, [])
            if analysis:
                citation = causal.program_citation(sources, node.reason)
                evidence = _program_evidence(research_case, [citation] if citation else [], source_index)
            else:
                evidence = _claim_evidence(sources)[:1]
            value = dict(key=node.key, parent_key=node.parent_key, ref=node.ref, text=node.text[:800],
                              occurred_at=node.occurred_at, published_at=node.published_at, depth=node.depth,
                              time_status=node.time_status, relation=note.relation if note else '',
                              expandable=False if analysis else _expandable(node, note), evidence=evidence,
                              stop_reason=(step.stop_reason if step else '') or (note.stop_reason if note else ''))
            if analysis:
                if node.parent_key and value['time_status'] == 'earlier' and not causal._program_child(node):
                    value['time_status'] = 'unknown'
                    if value['relation'] == 'possible_cause':
                        value['relation'] = 'context'
                value.update(queries=loads(step.queries_json, []) if step else [],
                    candidate_count=step.candidate_count if step else None,
                    retrieval_mode=step.retrieval_mode if step else '',
                    retrieval=loads(report.report_json, {}) if report else {},
                    job_state=job.state if job else '', job_error=job.error if job else '',
                    source_refs=[dict(url=source.get('url', ''), title=source.get('title', ''),
                        kind=source.get('kind', ''), published_at=source.get('published_at', ''),
                        modified_at=source.get('modified_at', ''), cited_quote=source.get('cited_quote', ''),
                        quote=source.get('quote', '')[:1500]) for source in sources if isinstance(source, dict)][:8],
                    notice='已有树中的线索与原摘录；evidence 为空时不能作为已核对的事实证据。')
                if node.depth and not job and not value['stop_reason']:
                    value['stop_reason'] = causal.program_leaf_reason(
                        {**value, 'expandable': bool(causal._program_child(node)) and node.depth < causal.MAX_DEPTH},
                        steps=completed_steps, node_count=len(rows))
            nodes.append(value)
        result = dict(case_key=case.key, run_id=run.run_id, event_at=case.event_at, text=case.text,
                    date_from=link.date_from, cutoff=run.cutoff or research_case.cutoff, nodes=nodes,
                    limits=dict(searches=0 if analysis else MAX_SEARCHES, searches_per_parent=0 if analysis else SEARCHES_PER_PARENT,
                                branches_per_depth=causal.BEAM_WIDTH, max_depth=causal.MAX_DEPTH,
                                max_children=causal.RESULTS))
        if analysis:
            result.update(analysis_only=True, allowed_tools=sorted(ANALYSIS_TOOLS),
                notice='程序搜索已结束。本轮仅阅读、核验和综合，不检索或修改树；搜索完成不表示因果已证实。')
        return result


def _search_result(row, lower, upper):
    return dict(search_id=row.key, node_key=row.node_key, query=row.query, date_from=row.date_from,
                date_to=row.date_to, effective_from=sqlite_utc_bound(lower) + 'Z',
                effective_until=sqlite_utc_bound(upper) + 'Z', candidates=loads(row.results_json, []))


def _search(engine, args):
    key = 'cquery_' + research.digest([args.run_id, args.node_key, args.query, args.date_from, args.date_to])[:32]
    with Session(engine) as session:
        research.locked(session)
        link, case, research_case, run = _scope(session, args.run_id, timed=True)
        node = _node(session, case.key, args.node_key)
        anchor, as_of, excluded = _bounds(session, case, node, research_case, run)
        lower, upper = _search_bounds(args, link.date_from, as_of)
        old = session.get(CausalAgentQuery, key)
        if old:
            if old.state == 'ready':
                return _search_result(old, lower, upper)
            raise ValueError(old.error or '相同检索正在执行或结果未确认')
        _check_branch(session, node, run)
        searched = list(session.exec(select(CausalAgentQuery.node_key)
                                     .where(CausalAgentQuery.case_key == case.key,
                                            CausalAgentQuery.run_id == run.run_id).limit(MAX_SEARCHES)))
        if len(searched) >= MAX_SEARCHES or searched.count(node.key) >= SEARCHES_PER_PARENT:
            raise ValueError('本轮的本地事件检索次数已达上限，请保存已有线索或结束说明')
        session.add(CausalAgentQuery(key=key, case_key=case.key, run_id=run.run_id, node_key=node.key,
                                     query=args.query, date_from=args.date_from, date_to=args.date_to))
        owner_case_id = research_case.case_id
        window = f'{args.date_from} 至 {args.date_to}'
        research.activity(session, research_case.case_id, 'causal_search_started',
                          f'查找前因（{window}）：{args.query}', run.run_id, key)
        session.commit()

    # Embedding and retrieval may take time. Release research and SQL locks
    # before performing the lookup or cancellation cannot fence this run.
    try:
        candidates = retrieve(engine, [args.query], lower=sqlite_utc_bound(lower),
                              upper=sqlite_utc_bound(upper), exclude=excluded)
        found = [result for item in candidates if item['ref'] not in excluded
                 and (result := _in_scope(item, lower, upper, anchor))][:MAX_RESULTS]
    except Exception:
        message = '本地前因检索失败，未保存候选'
        with Session(engine) as session:
            research.locked(session)
            row = session.get(CausalAgentQuery, key)
            row.state, row.error = 'failed', message
            research.activity(session, owner_case_id, 'causal_search_failed', f'{message}（{window}）', args.run_id, key)
            session.commit()
        raise ValueError(message) from None
    with Session(engine) as session:
        research.locked(session)
        row = session.get(CausalAgentQuery, key)
        try:
            _, _, current_case, _ = _scope(session, args.run_id)
        except (ValueError, KeyError):
            row.state, row.error = 'cancelled', '研究已停止，未保存本次检索结果'
            session.commit()
            raise ValueError(row.error) from None
        row.state, row.results_json = 'ready', dumps(found)
        research.activity(session, current_case.case_id, 'causal_search_completed',
                          f'已保存 {len(found)} 条本地前因线索（{window}）', args.run_id, key)
        session.commit()
        return _search_result(row, lower, upper)


def _stored_candidate(session, entry, link, run, node):
    query = session.get(CausalAgentQuery, entry.search_id)
    if (not query or query.case_key != link.case_key or query.run_id != run.run_id
            or query.node_key != node.key or query.state != 'ready'):
        raise ValueError('检索回执不属于当前节点与研究轮次')
    candidate = next((item for item in loads(query.results_json, []) if item['ref'] == entry.ref), None)
    if candidate is None:
        raise ValueError('候选编号未出现在这次检索结果中')
    return {**candidate, 'sources': [{**source, 'search_id': query.key} for source in candidate['sources']]}


def _quote_dates(value):
    dates = [part.group() for part in LITERAL.finditer(value)]
    for parts in CHINESE_DATE.findall(value):
        try:
            dates.append(date(*(int(part) for part in parts)).isoformat())
        except ValueError:
            continue
    for text in _ENGLISH_DATE.findall(value):
        try:
            format_string = '%d %B %Y' if text[0].isdigit() else '%B %d, %Y'
            dates.append(datetime.strptime(text, format_string).date().isoformat())
        except ValueError:
            continue
    return [point for value in dates if (point := interval(value))]


def _external_candidate(session, entry, research_case):
    source = session.get(ResearchSource, entry.source_id)
    if (not source or source.case_id != research_case.case_id
            or source.kind in {'search_snippet', 'event_metadata'}
            or not source_url(source.url) or '\\' in source.url):
        raise ValueError('请引用本课题已读取的原文资料')
    paragraph = next((item['text'] for item in loads(source.paragraphs_json, [])
                      if item['id'] == entry.paragraph_id), '')
    if entry.quote not in paragraph or entry.text not in paragraph:
        raise ValueError('事件文字和摘录必须是所引原文段落中的实际连续文本')
    if entry.occurred_at:
        point = interval(entry.occurred_at)
        if point is None or point not in _quote_dates(entry.quote):
            raise ValueError('发生日期须是引文明确记载的日期，不能确定时可留空')
    ref = 'source:' + research.digest([canonicalize_url(source.url), entry.paragraph_id, entry.quote])[:32]
    saved = dict(url=source.url, title=source.title, quote=entry.quote, published_at=source.published_at,
                 kind=source.kind, source_id=source.source_id, paragraph_id=entry.paragraph_id)
    return dict(ref=ref, text=entry.text, event_key='', occurred_at=entry.occurred_at,
                published_at=source.published_at, sources=[saved], similarity=0.)


def _retrospective(candidate, relation, anchor, upper, lower, cutoff):
    if relation not in {'context', 'document_reference'} or anchor is None:
        return None
    occurrence = interval(candidate['occurred_at'])
    published = interval(candidate['published_at'])
    if (occurrence is None or published is None or _earlier_than(occurrence, lower)
            or not _earlier_than(occurrence, min(anchor[0], upper))
            or published[0] < upper or published[1] > cutoff):
        return None
    return {**candidate, 'time_status': 'unknown',
            'sources': [{**source, 'retrospective': True} for source in candidate['sources']]}


def _local_quote(source, *, program=False):
    text = (source.get('cited_quote') or source['quote']) if program else source['quote']
    quote = max((line.strip() for line in text.splitlines() if '\x00' not in line),
                key=len, default='')
    # add_source uses paragraphs of at most 1500 characters; analysis citations
    # must remain a real continuous slice of the first registered paragraph.
    return quote[:1500] if program else quote


def _register_local_excerpt(session, research_case, run, ref, source, *, program_case_key=''):
    kinds = _PROGRAM_EXCERPT_KINDS if program_case_key else _LOCAL_EXCERPT_KINDS
    if source['kind'] not in kinds or not source_url(source['url']):
        return source
    # Research evidence cites a single real paragraph. Keep any wider
    # multiline excerpt intact in the tree and register a literal slice.
    quote = _local_quote(source, program=bool(program_case_key))
    if not quote:
        return source
    published = excerpt_time(source) if program_case_key else source['published_at']
    provenance = dict(content_kind='excerpt', source_kind=source['kind'], run_id=run.run_id,
                      candidate_ref=ref, published_at=published)
    if source.get('search_id'):
        provenance['search_id'] = source['search_id']
    if program_case_key:
        provenance['causal_case_key'] = program_case_key
        provenance['registration'] = 'existing_program_tree_excerpt'
        if source.get('modified_at'):
            provenance.update(first_published_at=source['published_at'], modified_at=source['modified_at'])
    try:
        row = research.add_source(
            session, research_case, title=source['title'], url=source['url'], text=quote,
            kind='causal_excerpt', published_at=published,
            provenance=provenance)
    except ValueError:
        # The research source cap must not discard verified tree evidence.
        return source
    return {**source, 'source_id': row.source_id, 'paragraph_id': 'P1', 'citation_quote': quote}


def register_program_sources(session, link, research_case, run):
    """Register existing literal tree excerpts at enqueue, without changing it.

    Program retrieval predates this research run, so no agent query receipt is
    invented. Eligible publisher and local excerpts enter the case; headlines
    and event metadata remain non-citable leads.
    """
    rows = session.exec(select(CausalNode.ref, CausalNode.reason, CausalNode.sources_json).where(
        CausalNode.case_key == link.case_key).order_by(CausalNode.depth, CausalNode.key).limit(causal.MAX_TREE_NODES))
    for ref, reason, sources_json in rows:
        source = causal.program_citation(loads(sources_json, []), reason)
        if source and all(isinstance(source.get(field), str) for field in ('kind', 'url', 'quote', 'title', 'published_at')):
            _register_local_excerpt(session, research_case, run, ref, source, program_case_key=link.case_key)


def _program_evidence(research_case, sources, source_index):
    evidence = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        source_id = source.get('source_id', '')
        paragraph_id = source.get('paragraph_id', '')
        quote = source.get('citation_quote', source.get('quote', ''))
        registered = source_index.get(source_id)
        if registered is None and source.get('kind') in _PROGRAM_EXCERPT_KINDS and isinstance(source.get('quote'), str):
            quote = _local_quote(source, program=True)
            # Runs submitted before 0.56 registered the wider excerpt. An
            # existing source remains citable if it contains this exact quote.
            legacy_quote = _local_quote(source)[:1500]
            for content in dict.fromkeys((quote, legacy_quote)):
                content_hash = hashlib.sha256(content.encode()).hexdigest()
                source_id = 'src_' + research.digest(
                    [research_case.case_id, source.get('url', ''), 'causal_excerpt', content_hash])[:24]
                registered = source_index.get(source_id)
                if registered is not None:
                    paragraph_id = 'P1'
                    break
        if registered is None or registered.kind in {'search_snippet', 'event_metadata'} or not quote:
            continue
        paragraph = next((row.get('text', '') for row in loads(registered.paragraphs_json, [])
                          if row.get('id') == paragraph_id), '')
        if isinstance(quote, str) and quote in paragraph:
            item = dict(source_id=registered.source_id, paragraph_id=paragraph_id, quote=quote)
            if item not in evidence:
                evidence.append(item)
    return evidence[:8]


def _claim_evidence(sources):
    return [dict(source_id=source['source_id'], paragraph_id=source['paragraph_id'],
                 quote=source.get('citation_quote', source['quote'])) for source in sources
            if source.get('source_id') and source.get('paragraph_id') and source.get('quote')]


def _merge_handoff_sources(historical, current):
    merged = {(source['url'], source['quote']): source for source in historical}
    for source in current:
        key = source['url'], source['quote']
        if key not in merged:
            merged[key] = source
        elif source.get('source_id') and not merged[key].get('source_id'):
            metadata = {name: source[name] for name in ('search_id', 'source_id', 'paragraph_id', 'citation_quote')
                        if name in source}
            merged[key] = {**merged[key], **metadata}
    # Both local and agent chronology return at most four sources per candidate.
    return list(merged.values())[:8]


def _save(engine, args):
    if not args.candidates and not args.stop_reason:
        raise ValueError('请提交候选或填写停止查找的原因')
    with Session(engine) as session:
        research.locked(session)
        link, case, research_case, run = _scope(session, args.run_id)
        node = _node(session, case.key, args.node_key)
        anchor, upper, excluded = _bounds(session, case, node, research_case, run)
        floor = interval(link.date_from)
        if floor is None:
            raise ValueError('调查的回溯起始日无效')
        lower = floor[0]
        if args.candidates:
            _check_branch(session, node, run)
        cutoff = min(research.timestamp(case.created_at), research.timestamp(run.cutoff or research_case.cutoff))
        children = {item.ref: item for item in session.exec(select(CausalNode).where(
            CausalNode.case_key == case.key, CausalNode.parent_key == node.key))}
        saved, seen = [], set()
        capacity = min(causal.RESULTS - len(children), causal.MAX_TREE_NODES - causal._count(session, case.key))
        for entry in args.candidates:
            external = isinstance(entry, ExternalCandidate)
            raw = (_external_candidate(session, entry, research_case) if external
                   else _stored_candidate(session, entry, link, run, node))
            ref = raw['ref']
            if ref in excluded:
                raise ValueError('候选与本节点或其祖先重复')
            if ref in seen:
                raise ValueError('本次提交包含重复候选')
            seen.add(ref)
            candidate = _in_scope(raw, lower, upper, anchor)
            if not candidate and external:
                candidate = _retrospective(raw, entry.relation, anchor, upper, lower, cutoff)
            if not candidate:
                raise ValueError('该资料或事件不在本节点的前置时间范围')
            child = children.get(ref)
            note = session.get(CausalAgentNote, child.key) if child else None
            if note and note.relation and note.relation != entry.relation:
                raise ValueError('已保存线索的关系类型不能由重复提交覆盖')
            sources = candidate['sources']
            if not external and (not note or not note.relation):
                sources = [_register_local_excerpt(session, research_case, run, ref, source) for source in sources]
            if child is None:
                if capacity <= 0:
                    raise ValueError('该节点或调查已达到线索数量上限')
                capacity -= 1
                child = CausalNode(key='cn_' + uuid4().hex, case_key=case.key, parent_key=node.key,
                                   ref=ref, text=candidate['text'], depth=node.depth + 1,
                                   event_key=candidate['event_key'], occurred_at=candidate['occurred_at'],
                                   published_at=candidate['published_at'], time_status=candidate['time_status'],
                                   reason=entry.reason, sources_json=dumps(sources),
                                   score=candidate.get('similarity', 0.))
                session.add(child)
            elif not note or not note.relation:
                sources = _merge_handoff_sources(loads(child.sources_json, []), sources)
                child.sources_json = dumps(sources)
            else:
                sources = loads(child.sources_json, [])
            if not note or not note.relation:
                note = note or CausalAgentNote(node_key=child.key, case_key=case.key)
                note.relation = entry.relation
                session.add(note)
            saved.append(dict(key=child.key, ref=ref, time_status=child.time_status,
                              relation=note.relation, evidence=_claim_evidence(sources)))
        if args.stop_reason:
            note = session.get(CausalAgentNote, node.key) or CausalAgentNote(node_key=node.key, case_key=case.key, relation='')
            note.stop_reason = args.stop_reason
            session.add(note)
        case.updated_at = utc_now_iso()
        research.activity(session, research_case.case_id, 'causal_step_saved',
                          f'已保存 {len(saved)} 条前因线索' + ('，已记录停止原因' if args.stop_reason else ''),
                          run.run_id, node.key)
        session.commit()
        return dict(case_key=case.key, node_key=node.key, saved=saved, stop_reason=args.stop_reason)


def call(engine, name, args):
    if name not in _TOOLS:
        raise ValueError('未知调查工具')
    try:
        values = _TOOLS[name][0].model_validate(args)
    except ValidationError:
        raise ValueError('调查工具参数格式无效，请核对工具定义') from None
    from ..research.causal_dispatch import require_allowed
    with Session(engine) as session:
        require_allowed(session, values.run_id, name, args)
    return {'causal_get_investigation': _get, 'causal_search_events': _search,
            'causal_save_step': _save}[name](engine, values)
