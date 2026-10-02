"""Short, serialized transactions; external I/O never holds the research lock."""
import hashlib
import json
import re
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import update
from sqlmodel import Session, select

from ..storage.models import Article, ArticleEventLink, utc_now_iso
from ..storage.articles import latest_version
from .models import (ResearchLock, ResearchStrategy, ResearchCase, ResearchRun, ResearchQuestion,
                     ResearchSource, ResearchClaim, ResearchReport, ResearchActivity, ResearchToolCall)
from .config import StrategyInput, DEFAULT_STRATEGIES

ACTIVE = {'queued', 'starting', 'running', 'waiting', 'stopping', 'uncertain'}
WRITABLE = {'starting', 'running', 'waiting', 'uncertain'}


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(dump(value).encode()).hexdigest()


def identifier(prefix):
    return prefix + '_' + uuid4().hex[:24]


def timestamp(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('时间必须包含时区')
    return dt.astimezone(timezone.utc)


def locked(session):
    session.exec(update(ResearchLock).where(ResearchLock.name == 'research').values(revision=ResearchLock.revision + 1))


def initialize(engine):
    with Session(engine, expire_on_commit=False) as s:
        if not s.get(ResearchLock, 'research'):
            s.add(ResearchLock(name='research'))
            s.commit()
        locked(s)
        for sid, name, questions in DEFAULT_STRATEGIES:
            if not s.get(ResearchStrategy, sid):
                value = StrategyInput(name=name, questions=questions).model_dump(exclude={'revision', 'enabled', 'name'})
                s.add(ResearchStrategy(strategy_id=sid, name=name, config_json=dump(value)))
        s.commit()


def activity(s, case_id, kind, message, run_id='', request_id=''):
    s.add(ResearchActivity(case_id=case_id, run_id=run_id, kind=kind, message=message[:4000], request_id=request_id))


def strategies(s):
    return [dict(strategy_id=r.strategy_id, name=r.name, revision=r.revision, enabled=r.enabled,
                 **json.loads(r.config_json)) for r in s.exec(select(ResearchStrategy).order_by(ResearchStrategy.strategy_id))]


def save_strategy(engine, strategy_id, payload):
    value = StrategyInput.model_validate(payload)
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', strategy_id):
        raise ValueError('策略编号只允许字母、数字、下划线和短横线')
    if any(not q.strip() or len(q) > 600 for q in value.questions):
        raise ValueError('每个研究问题需要 1–600 个字符')
    if any(not re.fullmatch(r'[a-zA-Z0-9.-]{1,253}', d) for d in value.preferred_domains):
        raise ValueError('优先来源请填写域名，不包含协议或路径')
    with Session(engine, expire_on_commit=False) as s:
        locked(s)
        row = s.get(ResearchStrategy, strategy_id)
        if row and row.revision != value.revision:
            raise ValueError('策略已被修改，请刷新后重试')
        if not row and value.revision:
            raise ValueError('新策略的版本应为 0')
        row = row or ResearchStrategy(strategy_id=strategy_id, name=value.name, config_json='{}', revision=0)
        row.name, row.enabled = value.name, value.enabled
        row.config_json = dump(value.model_dump(exclude={'revision', 'enabled', 'name'}))
        row.revision += 1
        s.add(row)
        s.commit()
        return next(r for r in strategies(s) if r['strategy_id'] == strategy_id)


def require_case(s, case_id):
    row = s.get(ResearchCase, case_id)
    if not row:
        raise KeyError('研究课题不存在')
    return row


def source_dict(row, full=False):
    data = row.model_dump()
    paragraphs = json.loads(data.pop('paragraphs_json'))
    data['provenance'] = json.loads(data.pop('provenance_json'))
    data['paragraph_count'] = len(paragraphs)
    if full:
        data['paragraphs'] = paragraphs
    return data


def add_source(s, case, *, title, url, text, kind, published_at='', provenance=None):
    text = str(text).replace('\x00', '').strip()[:100000]
    if not text:
        raise ValueError('资料正文为空')
    if published_at:
        try:
            date = timestamp(published_at)
            if date > timestamp(case.cutoff):
                raise ValueError('资料发布时间晚于课题截止时间')
        except ValueError as exc:
            if '晚于' in str(exc):
                raise
            published_at = ''
    h = hashlib.sha256(text.encode()).hexdigest()
    sid = 'src_' + digest([case.case_id, url, kind, h])[:24]
    old = s.get(ResearchSource, sid)
    if old:
        return old
    if len(list(s.exec(select(ResearchSource.source_id).where(ResearchSource.case_id == case.case_id)))) >= 120:
        raise ValueError('本课题已达到 120 份资料上限，请新建后续课题')
    chunks = []
    for block in re.split(r'\n\s*\n|\n', text):
        block = block.strip()
        for offset in range(0, len(block), 1500):
            chunks.append(block[offset:offset + 1500])
    paragraphs = [{'id': f'P{i+1}', 'text': block} for i, block in enumerate(chunks)]
    row = ResearchSource(source_id=sid, case_id=case.case_id, title=title[:1000], url=url,
                         kind=kind, content_hash=h, paragraphs_json=dump(paragraphs),
                         published_at=published_at, provenance_json=dump(provenance or {}))
    s.add(row)
    return row


def create_case(engine, payload):
    goal = str(payload.get('goal', '')).strip()
    if not goal or len(goal) > 6000:
        raise ValueError('请填写研究目的，最多 6000 字')
    keys = list(dict.fromkeys(payload.get('article_keys') or []))
    if any(not isinstance(k, str) for k in keys) or len(keys) > 12:
        raise ValueError('一次最多选择 12 条新闻')
    cutoff = payload.get('cutoff') or utc_now_iso()
    cutoff = timestamp(cutoff).isoformat().replace('+00:00', 'Z')
    if timestamp(cutoff) > datetime.now(timezone.utc):
        raise ValueError('研究截止时间不能晚于当前时间')
    date_from = payload.get('date_from') or ''
    if date_from:
        datetime.strptime(date_from, '%Y-%m-%d')
        if date_from > cutoff[:10]:
            raise ValueError('回溯起始日期不能晚于截止日期')
    with Session(engine, expire_on_commit=False) as s:
        locked(s)
        strategy = s.get(ResearchStrategy, payload.get('strategy_id', 'background'))
        if not strategy or not strategy.enabled:
            raise ValueError('研究策略不存在或已停用')
        event_key = payload.get('event_key') or ''
        if event_key and not keys:
            keys = list(s.exec(select(ArticleEventLink.article_key).where(ArticleEventLink.event_key == event_key).limit(12)))
        articles = []
        for key in keys:
            article = s.exec(select(Article).where(Article.article_key == key)).first()
            if not article:
                raise ValueError('所选新闻不存在')
            articles.append(article)
        if not articles:
            raise ValueError('请至少选择一条新闻作为研究起点')
        title = str(payload.get('title') or articles[0].title).strip()[:250]
        snapshot = dict(strategy_id=strategy.strategy_id, name=strategy.name, revision=strategy.revision,
                        **json.loads(strategy.config_json))
        case = ResearchCase(case_id=identifier('research'), title=title, goal=goal, article_keys_json=dump(keys),
                            event_key=event_key, strategy_json=dump(snapshot), date_from=date_from, cutoff=cutoff)
        s.add(case)
        for article in articles:
            version = latest_version(s, article.article_key)
            add_source(s, case, title=article.title, url=article.url, text=article.content or article.title,
                       kind='news_snapshot', published_at=article.published_at,
                       provenance={'article_key': article.article_key, 'article_version': version.revision if version else None,
                                   'independence_group': article.duplicate_of_article_key or article.article_key})
        activity(s, case.case_id, 'created', '已创建研究课题并保存新闻快照')
        s.commit()
        return detail(s, case.case_id)


def detail(s, case_id):
    case = require_case(s, case_id)
    data = case.model_dump()
    data['article_keys'] = json.loads(data.pop('article_keys_json'))
    data['strategy'] = json.loads(data.pop('strategy_json'))
    data['runs'] = [r.model_dump() for r in s.exec(select(ResearchRun).where(ResearchRun.case_id == case_id).order_by(ResearchRun.started_at.desc()))]
    data['questions'] = []
    for row in s.exec(select(ResearchQuestion).where(ResearchQuestion.case_id == case_id)):
        v = row.model_dump(); v['claim_ids'] = json.loads(v.pop('claim_ids_json')); data['questions'].append(v)
    data['sources'] = [source_dict(r) for r in s.exec(select(ResearchSource).where(ResearchSource.case_id == case_id))]
    data['claims'] = []
    for row in s.exec(select(ResearchClaim).where(ResearchClaim.case_id == case_id).order_by(ResearchClaim.created_at)):
        v = row.model_dump(); v['evidence'] = json.loads(v.pop('evidence_json')); data['claims'].append(v)
    data['reports'] = []
    for row in s.exec(select(ResearchReport).where(ResearchReport.case_id == case_id).order_by(ResearchReport.version.desc())):
        v = row.model_dump(); v['sections'] = json.loads(v.pop('sections_json')); v['gaps'] = json.loads(v.pop('gaps_json')); data['reports'].append(v)
    data['activity'] = [r.model_dump() for r in s.exec(select(ResearchActivity).where(ResearchActivity.case_id == case_id).order_by(ResearchActivity.id.desc()).limit(150))]
    return data


def active_run(s, run_id, *, enforce_time=True):
    run = s.get(ResearchRun, run_id)
    if not run:
        raise ValueError('研究执行不存在')
    case = require_case(s, run.case_id)
    if case.active_run_id != run_id or run.status not in WRITABLE:
        raise ValueError('本次研究已停止或被后续执行替代，请勿继续写入')
    if enforce_time:
        minutes = json.loads(case.strategy_json)['max_minutes']
        if (datetime.now(timezone.utc) - timestamp(run.started_at)).total_seconds() > minutes * 60:
            raise ValueError('本轮时间预算已用尽；请保存部分报告并列出未解决问题')
    return case, run


def save_question(s, case, args):
    key = str(args['key'])
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', key):
        raise ValueError('问题 key 格式无效')
    status = args.get('status', 'open')
    if status not in {'open', 'researching', 'answered', 'gap'}:
        raise ValueError('问题状态无效')
    text, answer = str(args['text']).strip(), str(args.get('answer', ''))
    if not text or len(text) > 1000 or len(answer) > 6000:
        raise ValueError('问题或答案长度无效')
    claims = args.get('claim_ids', [])
    check_claims(s, case.case_id, claims)
    if status == 'answered' and not claims:
        raise ValueError('已回答的问题需要关联判断；证据不足时使用 gap')
    qid = case.case_id + ':' + key
    row = s.get(ResearchQuestion, qid)
    if not row and len(list(s.exec(select(ResearchQuestion.question_id).where(ResearchQuestion.case_id == case.case_id)))) >= 60:
        raise ValueError('每个课题最多 60 个研究问题')
    row = row or ResearchQuestion(question_id=qid, case_id=case.case_id, key=key, text=text)
    row.text, row.status, row.answer, row.claim_ids_json = text, status, answer, dump(claims)
    row.updated_at = utc_now_iso()
    s.add(row)
    return {'question_id': qid, 'status': status}


def check_claims(s, case_id, ids):
    if not isinstance(ids, list) or len(ids) > 60:
        raise ValueError('判断引用格式无效')
    for cid in ids:
        row = s.get(ResearchClaim, cid)
        if not row or row.case_id != case_id:
            raise ValueError('引用的判断不属于当前研究课题')


def save_claim(s, case, run, args):
    text, kind = str(args['text']).strip(), args.get('kind', 'fact')
    if not text or len(text) > 4000 or kind not in {'fact', 'attributed', 'analysis', 'unverified'}:
        raise ValueError('判断文本或类型无效')
    evidence = args.get('evidence', [])
    if not isinstance(evidence, list) or len(evidence) > 20:
        raise ValueError('证据数量无效')
    if kind != 'unverified' and not evidence:
        raise ValueError('事实、各方说法和分析都需要来源；缺少证据请用 unverified')
    clean = []
    for item in evidence:
        source = s.get(ResearchSource, item['source_id'])
        if not source or source.case_id != case.case_id or source.kind == 'search_snippet':
            raise ValueError('证据必须来自本课题已读取的正文，不能引用搜索摘要')
        paragraph = next((p for p in json.loads(source.paragraphs_json) if p['id'] == item['paragraph_id']), None)
        quote = str(item.get('quote', '')).strip()
        if not paragraph or not quote or quote not in paragraph['text']:
            raise ValueError('证据摘录必须是所引段落中实际存在的连续文本')
        relation = item.get('relation', 'supports')
        if relation not in {'supports', 'contradicts', 'context'}:
            raise ValueError('证据关系无效')
        clean.append(dict(source_id=source.source_id, paragraph_id=paragraph['id'], quote=quote, relation=relation))
    cid = 'claim_' + digest([case.case_id, text, kind, clean, args.get('occurred_at', '')])[:24]
    if not s.get(ResearchClaim, cid):
        if len(list(s.exec(select(ResearchClaim.claim_id).where(ResearchClaim.case_id == case.case_id)))) >= 200:
            raise ValueError('每个课题最多 200 条判断')
        s.add(ResearchClaim(claim_id=cid, case_id=case.case_id, run_id=run.run_id, text=text, kind=kind,
                            evidence_json=dump(clean), occurred_at=str(args.get('occurred_at', ''))[:100]))
    return {'claim_id': cid, 'review_status': 'draft'}


def save_report(s, case, run, args):
    title = str(args['title']).strip()[:250]
    sections = args['sections']
    gaps = args.get('gaps', [])
    completeness = args.get('completeness', 'partial')
    if not title or not isinstance(sections, list) or not 1 <= len(sections) <= 20:
        raise ValueError('报告需要标题和 1–20 个章节')
    if completeness not in {'complete', 'partial'} or not isinstance(gaps, list) or len(gaps) > 60:
        raise ValueError('报告完成状态或待查问题无效')
    clean = []
    for section in sections:
        heading, body = str(section['heading']), str(section['body'])
        if not heading.strip() or len(heading) > 200 or len(body) > 10000:
            raise ValueError('报告章节长度无效')
        claims = section.get('claim_ids', [])
        check_claims(s, case.case_id, claims)
        clean.append(dict(heading=heading, body=body, claim_ids=claims))
    if completeness == 'complete' and not any(v['claim_ids'] for v in clean):
        raise ValueError('完整报告必须关联证据判断')
    clean_gaps = [str(x)[:2000] for x in gaps]
    h = digest([title, clean, clean_gaps, completeness, run.cutoff or case.cutoff])
    old = s.exec(select(ResearchReport).where(ResearchReport.case_id == case.case_id, ResearchReport.content_hash == h)).first()
    if old:
        return {'report_id': old.report_id, 'version': old.version}
    reports = list(s.exec(select(ResearchReport).where(ResearchReport.case_id == case.case_id)))
    row = ResearchReport(report_id=identifier('report'), case_id=case.case_id, run_id=run.run_id,
                         version=max([r.version for r in reports], default=0) + 1, title=title,
                         sections_json=dump(clean), gaps_json=dump(clean_gaps), completeness=completeness,
                         cutoff=run.cutoff or case.cutoff, content_hash=h)
    s.add(row)
    return {'report_id': row.report_id, 'version': row.version}


def export_report(s, case_id, report_id):
    case = require_case(s, case_id)
    row = s.get(ResearchReport, report_id)
    if not row or row.case_id != case_id:
        raise KeyError('报告不存在')
    lines = [f'# {row.title}', '', f'版本：{row.version} · 资料截止：{row.cutoff or case.cutoff} · 人工审核：待审核', '']
    seen = set()
    for section in json.loads(row.sections_json):
        lines += ['## ' + section['heading'], '', section['body'], '']
        for cid in section['claim_ids']:
            if cid in seen:
                continue
            seen.add(cid)
            claim = s.get(ResearchClaim, cid)
            lines += [f'- 判断（{claim.kind}）：{claim.text}']
            for evidence in json.loads(claim.evidence_json):
                source = s.get(ResearchSource, evidence['source_id'])
                lines += [f'  - {source.title} · {evidence["paragraph_id"]} · {source.url}',
                          f'    摘录：{evidence["quote"]}']
        lines += ['']
    lines += ['## 待查事项', ''] + ['- ' + str(g) for g in json.loads(row.gaps_json)]
    return '\n'.join(lines)
