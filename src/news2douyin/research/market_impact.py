"""Prepare evidence snapshots for a user-initiated market impact study."""
from urllib.parse import quote

from sqlmodel import select

from ..events.workbench import canonical_event, require_event
from ..selection.source_content import source_url
from ..storage.articles import latest_version
from ..storage.models import ArticleEventLink, ArticleVersion, EventMoment, EventState
from ..storage.sql import json_text
from ..storage.utils import loads
from ..workbench.service import candidate_material
from ..world_radar.market import classify
from . import service
from .materials import original_body, source_document
from .models import ResearchCase, ResearchReport, ResearchRun

QUESTIONS = [
    '原始材料证实了什么行动？请区分宣布、落实、恢复和背景，优先核对直接来源。',
    '这个动作通过什么环节影响哪些地区的供给、需求、物流或库存？分别说明证据与待核假设。',
    '最该查 TradeView 的哪些商品指标？核对代码、单位、频率、窗口、覆盖和最新修订口径。',
    '有哪些竞争解释、反例和数据缺口？哪项观测最能区分这些解释？',
]
GOAL = (
    '先核查事件及其证据，识别商品市场传导路径和可能替代解释。先调用 research_get_case，检查起点来源及保存的上下文。'
    '确定具体商品和地区后，使用 research_commodity_series 查询 TradeView 实际收录的序列，'
    '使用 research_commodity_observations 读取可引用的市场观测并保存于课题。'
    '日期使用对应市场统计期，记录不同单位、频率、观测空值和最新修订口径。'
    '把事实证据、传导假设、市场同期变化和未查到的数据写入中文报告。'
    '如未定位商品或序列、或无法核实传导，保留缺口并提交阶段报告。单凭先后或同步变化不能确认因果。'
)


def _version_document(version):
    document = loads(version.payload_json, {})
    if not isinstance(document, dict):
        return None
    title = document.get('title', '')
    url = source_url(document.get('url'))
    if not isinstance(title, str) or not title.strip() or not url or '\\' in url:
        return None
    document = source_document(document)
    document.update(article_key=version.article_key, revision=version.revision,
                    content_hash=version.content_hash, url=url)
    if not original_body(document) and document.get('source_kind') not in {'event_metadata', 'newsnow_metadata'}:
        document.update(material_kind='candidate_lead', source_kind='search_snippet')
    body = document.get('content')
    document['content'] = body if isinstance(body, str) else ''
    return document


def _event_context(session, key, moment_key):
    key = canonical_event(session, key)
    event = require_event(session, key)
    state = session.get(EventState, key)
    title = (state.title_override if state else '') or event.event_title
    if moment_key:
        moment = session.get(EventMoment, moment_key)
        if not moment or moment.event_key != key or moment.deleted or not moment.reviewed:
            raise KeyError('未找到此事件已核对的进展')
        title = moment.title or title
        materials = []
        for proof in loads(moment.sources_json, [])[:12]:
            if not isinstance(proof, dict):
                continue
            version = session.exec(select(ArticleVersion).where(
                ArticleVersion.article_key == proof.get('article_key'),
                ArticleVersion.revision == proof.get('revision'),
            )).first()
            if version and version.content_hash == proof.get('content_hash') and (doc := _version_document(version)):
                excerpt = proof.get('excerpt') or ''
                if isinstance(excerpt, str) and excerpt in doc['content']:
                    doc['checked_excerpt'] = excerpt
                materials.append(doc)
    else:
        keys = session.exec(select(ArticleEventLink.article_key)
            .where(ArticleEventLink.event_key == key).order_by(ArticleEventLink.id).limit(12)).all()
        materials = [doc for article_key in dict.fromkeys(keys) if (version := latest_version(session, article_key))
                     and (doc := _version_document(version))]
    docs = {doc['article_key']: doc for doc in materials}
    return dict(title=title, event_key=key, moment_key=moment_key, candidate_key='',
                date=(moment.date_start if moment_key else '') or '',
                path='/events/' + quote(key, safe=''), documents=list(docs.values()))


def context(session, storage_root, *, event_key='', candidate_key='', moment_key=''):
    if bool(event_key) == bool(candidate_key) or (moment_key and not event_key):
        raise ValueError('请选择一个事件或一条有出处的候选作为研究起点')
    if event_key:
        result = _event_context(session, event_key, moment_key)
    else:
        document, _ = candidate_material(session, storage_root, candidate_key)
        if not source_url(document['url']):
            raise ValueError('此线索需要先补可核对的来源')
        document['article_key'] = document['article_key'] or 'lead_' + service.digest(candidate_key)[:24]
        result = dict(title=document['title'], event_key='', moment_key='', candidate_key=candidate_key, date='',
                      path='/organize?candidate=' + quote(candidate_key, safe=''), documents=[document])
    if not result['documents']:
        raise ValueError('此事件尚无可引用的来源版本，请先加入报道')
    result['hypothesis'] = next((item for doc in result['documents']
        if (item := classify(doc['title'], (doc.get('checked_excerpt') or doc['content'])[:520]))), None)
    result['receipt'] = service.digest(result)
    return result


def create_case(engine, selection):
    reference = {key: selection[key] for key in ('event_key', 'moment_key', 'candidate_key', 'date')}
    reference['hypothesis'] = selection['hypothesis']
    case_id = 'research_' + service.digest(['market-impact', selection['receipt']])[:24]
    documents = selection['documents']
    return service.create_case(engine, dict(title='市场影响研究 · ' + selection['title'][:220],
        goal=GOAL, article_keys=[doc['article_key'] for doc in documents], event_key=selection['event_key']),
        frozen_sources=documents, fixed_case_id=case_id,
        budget_override=dict(name='市场影响研究', questions=QUESTIONS, market_impact=reference))


def related(session, event_key):
    query = select(ResearchCase.case_id, ResearchCase.title, ResearchCase.active_run_id).where(
        ResearchCase.event_key == event_key,
        json_text(ResearchCase.strategy_json, 'market_impact').is_not(None),
    ).order_by(ResearchCase.updated_at.desc(), ResearchCase.case_id).limit(5)
    result = []
    for case_id, title, active_run_id in session.exec(query):
        run = session.get(ResearchRun, active_run_id) if active_run_id else None
        report = session.exec(select(ResearchReport.report_id).where(ResearchReport.case_id == case_id)
                              .order_by(ResearchReport.version.desc()).limit(1)).first()
        result.append(dict(case_id=case_id, title=title, status=run.status if run else 'draft', report_id=report))
    return result
