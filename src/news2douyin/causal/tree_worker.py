"""One evidence-bounded step of the program-controlled historical tree.

The durable queue, leases and breadth/depth scheduling belong to service.py.
This module holds no SQL connection while calling either configured model or
retrieval boundary. Hypotheses are search inputs, never generated graph facts.
"""
from dataclasses import dataclass, field
from math import isfinite
from time import monotonic

from sqlmodel import Session

from ..llm.client.embedding import EmbeddingError
from . import history, model, retrieval, service
from .chronology import eligible, interval, sqlite_utc_bound
from .models import CausalInvestigation, CausalNode


RELATIONS = frozenset(('possible_cause', 'context', 'contradicts', 'document_reference'))
RETRIEVAL_MODES = frozenset(('lexical', 'lexical_embedding'))


@dataclass(frozen=True)
class ProgramStep:
    results: list[dict]
    queries: list[str]
    candidate_count: int
    stop_reason: str
    retrieval_mode: str = ''
    retrieval_report: dict = field(default_factory=dict)


class ProgramStepFailure(model.CausalModelError):
    """Safe failure plus already obtained progress for the durable step record."""

    def __init__(self, message, *, queries=(), candidate_count=0, retrieval_mode='', retrieval_report=None):
        super().__init__(message)
        self.queries = list(queries)
        self.candidate_count = candidate_count
        self.retrieval_mode = retrieval_mode
        self.retrieval_report = retrieval_report or {}


def _snapshot(engine, claimed):
    from .models import CausalProgram

    with Session(engine) as session:
        plan = session.get(CausalProgram, claimed.case_key)
        case = session.get(CausalInvestigation, claimed.case_key)
        node = session.get(CausalNode, claimed.node_key)
        if not plan or not case or not node or node.case_key != case.key:
            raise ProgramStepFailure('程序调查范围不存在，未进行检索。')
        floor = interval(plan.date_from)
        created = interval(case.created_at)
        if floor is None or created is None:
            raise ProgramStepFailure('程序调查日期范围无效，未进行检索。')
        target, lower, upper = node.text, floor[0], created[0]
        anchor, excluded, visited = None, set(), set()
        current = node
        while current:
            if current.key in visited or len(visited) > service.MAX_DEPTH:
                raise ProgramStepFailure('程序调查节点链无效，未进行检索。')
            visited.add(current.key)
            excluded.update(value for value in (current.ref, current.event_key) if value)
            point = interval(current.occurred_at) or interval(current.published_at)
            if point is not None:
                anchor = anchor or point
                # A day-only parent is bounded by the START of that day. Do
                # not turn same-day or later evidence into preceding evidence.
                upper = min(upper, point[0])
            if not current.parent_key:
                break
            current = session.get(CausalNode, current.parent_key)
            if current is None or current.case_key != case.key:
                raise ProgramStepFailure('程序调查节点链无效，未进行检索。')
        if anchor is None:
            raise ProgramStepFailure('请先确认目标日期，再进行程序调查。')
    return target, anchor, lower, upper, excluded


def _before_floor(point, lower):
    return point is not None and point[0] < lower and point[1] <= lower


def _eligible(item, anchor, lower, upper):
    if (not isinstance(item, dict) or not isinstance(item.get('ref'), str) or not item['ref']
            or not isinstance(item.get('text'), str) or not item['text'].strip()
            or not isinstance(item.get('sources'), list)):
        raise ProgramStepFailure('本轮程序召回结果格式无效，未保存候选。')
    if _before_floor(interval(item.get('occurred_at', '')), lower):
        return None
    sources = [source for source in item['sources'] if isinstance(source, dict)
               and isinstance(source.get('quote'), str) and source['quote'].strip()
               and not _before_floor(interval(source.get('published_at', '')), lower)]
    # eligible() conservatively keeps unknown dates unknown and removes every
    # source first published at/after the strict parent boundary.
    value = {**item, 'event_key': item.get('event_key', ''),
             'occurred_at': item.get('occurred_at', ''),
             'published_at': item.get('published_at', ''), 'sources': sources}
    return eligible(value, anchor, upper)


def _metadata(rows):
    count, modes = len(rows), set()
    for item in rows:
        metadata = item.get('retrieval') if isinstance(item, dict) else None
        if not isinstance(metadata, dict):
            continue
        size = metadata.get('pool_count')
        if type(size) is int and 0 <= size <= 100_000:
            count = max(count, size)
        mode = metadata.get('mode')
        if mode in RETRIEVAL_MODES:
            modes.add(mode)
    return count, next(iter(modes)) if len(modes) == 1 else ''


def investigate(engine, claimed, *, storage_root=None):
    """Propose hypotheses, recall/re-embed evidence, then rank exact candidates."""
    target, anchor, lower, upper, excluded = _snapshot(engine, claimed)
    if lower >= upper:
        return ProgramStep([], [], 0, '已到达调查最早日期，本分支停止。')
    queries, count, mode, report = [], 0, '', {}
    last_renewal = monotonic()

    def checkpoint(*, force=False):
        nonlocal last_renewal
        now = monotonic()
        if force or now - last_renewal >= 30:
            service.renew(engine, claimed)
            last_renewal = now

    try:
        settings = model.configure()
        bounds = dict(lower=sqlite_utc_bound(lower), upper=sqlite_utc_bound(upper))
        queries = model.propose_hypotheses(settings, target, **bounds)
        if not queries:
            return ProgramStep([], [], 0, '归因模型未提出可核查的前因假说，本分支停止。')
        service.renew(engine, claimed, hypotheses=queries)
        pool = retrieval.collect_tree_pool(engine, queries, **bounds, exclude=excluded,
                                           event_text=target, checkpoint=checkpoint)
        report = dict(local=pool.local, history=dict(status='skipped'))
        # A nearest neighbor can always be returned from a populated index.
        # It alone must not suppress the existing archive lookup.
        if pool.local.get('status') == 'gap' or not pool.local.get('keyword_dated_count', pool.local.get('dated_evidence_count')):
            trigger = 'local_gap' if pool.local.get('status') == 'gap' else 'no_dated_candidates'
            if trigger != 'local_gap' and pool.local.get('dated_evidence_count'):
                trigger = 'no_dated_keyword_candidates'
            checkpoint(force=True)
            kwargs = {'engine': engine, 'storage_root': storage_root} if storage_root is not None else {}
            supplement = history.search_history(queries, **bounds, event_text=target, checkpoint=checkpoint, **kwargs)
            report['history'] = {**supplement.diagnostics, 'trigger': trigger}
            historical = supplement.candidates
        else:
            historical = []
        ranked = retrieval.rank_tree_pool(engine, pool, historical=historical, checkpoint=checkpoint)
        report['local'] = ranked.local
        report['history'].update(eligible_count=ranked.history_eligible_count,
                                 ranked_count=ranked.history_ranked_count)
        raw = ranked.rows
        if not isinstance(raw, list):
            raise ProgramStepFailure('本轮程序召回结果格式无效，未保存候选。')
        count, mode = _metadata(raw)
        count = max(count, pool.local.get('pool_count', 0))
        mode = ranked.mode or mode
        candidates, refs = [], set()
        for item in raw:
            candidate = _eligible(item, anchor, lower, upper)
            if candidate and candidate['ref'] not in excluded and candidate['ref'] not in refs:
                refs.add(candidate['ref'])
                candidates.append(candidate)
        if len(candidates) > model.MAX_CANDIDATES:
            raise ProgramStepFailure('本轮程序召回超出单步候选上限，未保存候选。')
        report.update(ranked_count=len(candidates), selected_count=0)
        if not candidates:
            if report['history'].get('status') == 'error':
                raise ProgramStepFailure('本地没有合格候选，GDELT 历史补查失败，可重新查找。')
            if report['history'].get('status') == 'archive_gap':
                return ProgramStep([], queries, count, '调查时间窗存在历史档案缺口，可补档后继续。', mode, report)
            return ProgramStep([], queries, count,
                '本轮未取得符合日期与出处要求的候选。', mode, report)
        checkpoint()
        decisions = model.rank(settings, target, candidates, hypotheses=queries)
        by_ref = {item['ref']: item for item in candidates}
        results, saved_refs = [], set()
        for decision in decisions:
            ref = decision.get('ref') if isinstance(decision, dict) else None
            if ref not in by_ref or ref in saved_refs:
                raise ProgramStepFailure('归因模型返回了无效或重复的候选编号，未保存结果。')
            relation = decision.get('relation', 'context')
            score, reason = decision.get('score'), decision.get('reason')
            if (relation not in RELATIONS or type(score) not in (int, float) or not isfinite(score)
                    or not 0 <= score <= 1 or not isinstance(reason, str) or not reason.strip()):
                raise ProgramStepFailure('归因模型解释或关系类型无效，未保存结果。')
            saved_refs.add(ref)
            # The model may explain/order a supplied candidate. It may never
            # replace its source, text, event identity or either time clock.
            results.append({**by_ref[ref], 'reason': reason, 'score': float(score), 'relation': relation})
        results.sort(key=lambda item: (item['time_status'] != 'earlier', -item['score'], item['ref']))
        results = results[:service.RESULTS]
        report['selected_count'] = len(results)
        stop = ('归因模型未找到有可核查依据的解释候选，本分支停止。' if not results else
                '候选的前置时间尚未确认，保留线索并停止自动展开。'
                if all(item['time_status'] != 'earlier' for item in results) else '')
        if results and not stop and not any(
            item['time_status'] == 'earlier' and any(
                source.get('quote') and source.get('kind') != 'local_headline'
                for source in item['sources']) for item in results
        ):
            stop = '候选只有标题线索，缺少可核查的原文依据，停止自动展开。'
        return ProgramStep(results, queries, count, stop, mode, report)
    except service.CausalLeaseLost:
        raise
    except EmbeddingError:
        raise ProgramStepFailure('事件向量服务配置或请求失败，可在原节点重试。', queries=queries,
                                 candidate_count=count, retrieval_mode=mode, retrieval_report=report) from None
    except model.CausalModelError as exc:
        raise ProgramStepFailure(str(exc), queries=queries, candidate_count=count,
                                 retrieval_mode=mode, retrieval_report=report) from None
    except Exception:
        raise ProgramStepFailure('本轮程序调查检索失败，稍后可重试。', queries=queries,
                                 candidate_count=count, retrieval_mode=mode, retrieval_report=report) from None
