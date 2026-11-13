"""Versioned, budgeted section retrieval over the same typed relations as FalkorDB."""
from collections import defaultdict
from datetime import datetime, timezone
import json
import re
import unicodedata

from sqlalchemy import func
from sqlmodel import select
from ..desk.models import WatchEvent
from ..events.service import timestamp
from ..desk.rules import ALIASES
from ..storage.models import Article, ArticleVersion, ArticleEventLink
from .models import TextMention, MentionReview, SurveyPacket
from .outline import points
from .schema import LABELS

MAX_ARTICLES, BUDGET = 250, 12
STOP = {'哪些','什么','是否','如何','有何','情况','问题','发生','了什','么？','the','what','when','are','was','and','did','has','who','how'}


def terms(text):
    value=unicodedata.normalize('NFKC',text).casefold()
    words=set(re.findall(r'[a-z0-9]{2,}',value))
    for phrase in re.findall(r'[\u3400-\u9fff]+',value):
        words.update(phrase[i:i+2] for i in range(len(phrase)-1))
    for key,aliases in ALIASES.items():
        if key in value or any((a in value if not a.isascii() else
                re.search(r'(?<![a-z0-9])'+re.escape(a)+r'(?![a-z0-9])',value)) for a in aliases):
            words.add(key)
    return words-STOP


def cutoff_date(day=''):
    now=datetime.now(timezone.utc)
    if not day:
        return now.isoformat(timespec='seconds').replace('+00:00','Z')
    try:
        end=datetime.strptime(day,'%Y-%m-%d').replace(hour=23,minute=59,second=59,tzinfo=timezone.utc)
    except ValueError:
        raise ValueError('截止日期格式无效') from None
    if end.date()>now.date():
        raise ValueError('截止日期不能晚于今天')
    return min(now,end).isoformat(timespec='seconds').replace('+00:00','Z')


def material(s,watch,cutoff):
    from ..desk.service import current_membership
    member=select(ArticleEventLink.article_key).join(WatchEvent,WatchEvent.event==ArticleEventLink.event_key
        ).where(WatchEvent.watch==watch,WatchEvent.choice!='exclude',current_membership())
    at=select(ArticleVersion.article_key,func.max(ArticleVersion.revision).label('revision')
        ).where(ArticleVersion.article_key.in_(member),ArticleVersion.observed_at<=cutoff
        ).group_by(ArticleVersion.article_key).subquery()
    versions=s.exec(select(ArticleVersion).join(at,(ArticleVersion.article_key==at.c.article_key)&
        (ArticleVersion.revision==at.c.revision)).order_by(ArticleVersion.observed_at.desc(),
        ArticleVersion.article_key).limit(MAX_ARTICLES+1)).all()
    bounded=len(versions)>MAX_ARTICLES
    versions=versions[:MAX_ARTICLES]
    actions=defaultdict(list)
    for row,review in s.exec(select(TextMention,MentionReview).outerjoin(MentionReview,MentionReview.id==TextMention.id)
            .where(TextMention.version_id.in_([v.id for v in versions])).order_by(TextMention.id)):
        if not review or review.decision!='rejected':
            action=json.loads(row.data)
            actions[row.version_id].append((action,row.signature,review.decision if review else 'pending'))
    items=[]
    for v in versions:
        doc=json.loads(v.payload_json)
        published=doc.get('published_at') or ''
        publication=timestamp(published)
        if publication and publication>timestamp(cutoff):
            continue
        title=doc.get('title','').strip()
        header=dict(article=v.article_key,revision=v.revision,title=title,
            observed=v.observed_at,when=published or v.observed_at)
        for action,signature,review in actions[v.id]:
            tag=LABELS[action['kind']]+' · '+LABELS[action['status']]
            items.append(dict(header,quote=action['quote'],field=action['field'],start=action['start'],
                end=action['end'],mention=action['id'],tag=tag,signature=signature,review=review,
                tokens=terms(action['quote']+' '+tag)))
        # Keep a lexical channel even when the action vocabulary has no matching trigger.
        # A source passage can support retrieval without a model-inferred relation.
        raw=doc.get('content') or ''
        texts=[('title',0,title[:600])] if title else []
        for paragraph in re.finditer(r'[^\r\n]+',raw[:4000]):
            if len(texts)>=3:
                break
            quote=paragraph.group()[:550]
            if quote.strip():
                texts.append(('content',paragraph.start(),quote))
        for field,offset,quote in texts:
            items.append(dict(header,quote=quote,field=field,start=offset,end=offset+len(quote),
                mention='',tag='',signature='',review='pending',tokens=terms(quote)))
    return items,len(versions),bounded


def build(s,w,cutoff,point_key=''):
    items,scanned,truncated=material(s,w.key,cutoff)
    # Prefer checked source interpretations within a tie. A denial remains eligible.
    items.sort(key=lambda r:(r['review']=='accepted',r['when']),reverse=True)
    tree=points(s,w.key,cutoff)
    if point_key:
        tree=[p for p in tree if p.key==point_key]
    sections=[]
    for node in tree[:12]:
        query=terms(node.text)
        ranked=sorted(((len(query & r['tokens']),i) for i,r in enumerate(items)),
                      key=lambda pair:(-pair[0],pair[1]))
        sections.append(dict(key=node.key,parent=node.parent,title=node.text,origin='长期问题',
            pool=[i for score,i in ranked if score],evidence=[]))
    if not point_key:
        groups=defaultdict(list)
        for i,r in enumerate(items):
            if r['tag']:
                groups[r['tag']].append(i)
        ordered=sorted(groups,key=lambda k:(-len({items[i]['title'].casefold() for i in groups[k]}),k))
        for tag in ordered[:5]:
            sections.append(dict(key='',parent=w.key,title=tag,origin='材料分组建议',pool=groups[tag],evidence=[]))
        sections.append(dict(key='',parent=w.key,title='专题资料池',origin='专题范围命中',pool=list(range(len(items))),evidence=[]))
    chosen={}
    def attach(section, i, reason):
        r=items[i]
        if r['article'] not in chosen and len(chosen)>=BUDGET:
            return False
        if any(x['article']==r['article'] or x['quote'].casefold()==r['quote'].casefold()
               or x['title'].casefold()==r['title'].casefold() for x in section['evidence']):
            return False
        chosen[r['article']]=r['revision']
        public={k:v for k,v in r.items() if k not in ('tokens','signature','observed')}
        section['evidence'].append(dict(public,basis=reason))
        return True
    # First reserve one direct item for each section. Then spend remaining capacity.
    for passes in range(2):
        for section in sections:
            for i in section['pool']:
                reason={'长期问题':'问题与原文词项重合','材料分组建议':'相同动作类型与状态','专题范围命中':'已归入专题'}[section['origin']]
                if attach(section,i,reason):
                    break
    # One typed hop, restricted to current topic membership and the same cutoff.
    neighbors=defaultdict(list)
    signatures={}
    for i,r in enumerate(items):
        if r['signature']:
            neighbors[r['signature']].append(i)
            signatures[r['mention']]=r['signature']
    for section in sections:
        for seed in section['evidence'][:1]:
            for i in neighbors.get(signatures.get(seed['mention']),[]):
                if attach(section,i,'一跳候选：同动作、状态、日、角色与地点'):
                    break
        pool=section.pop('pool')
        section['gap']='' if section['evidence'] else ('本轮资料预算不足' if pool else '未找到可定位的直接材料')
    return dict(watch=w.key,cutoff=cutoff,sections=sections,selected=chosen,budget=BUDGET,
                scanned=scanned,truncated=truncated,remaining_questions=max(0,len(tree)-12))


def freeze(s, plan):
    docs=[]
    for key,revision in plan['selected'].items():
        version=s.exec(select(ArticleVersion).where(ArticleVersion.article_key==key,ArticleVersion.revision==revision)).one()
        doc=json.loads(version.payload_json)
        parent=s.exec(select(Article.duplicate_of_article_key).where(Article.article_key==key)).first()
        docs.append(dict(doc,article_key=key,revision=revision,duplicate_of_article_key=parent or ''))
    return docs


def bind(s, case, plan):
    """Bind chosen passages to research source/paragraph IDs before dispatch."""
    from ..research.models import ResearchSource
    from ..storage.utils import loads
    sources={}
    versions={(d['article_key'],d['revision']):d for d in freeze(s,plan)}
    for src in s.exec(select(ResearchSource).where(ResearchSource.case_id==case.case_id)):
        origin=loads(src.provenance_json,{})
        sources[(origin.get('article_key'),origin.get('article_version'))]=src
    lines=[]
    for section in plan['sections']:
        cites=[]
        for e in section['evidence']:
            src=sources.get((e['article'],e['revision']))
            if not src:
                continue
            doc=versions.get((e['article'],e['revision']))
            if not doc:
                continue
            title,content=doc.get('title',''),doc.get('content','')
            prefix=title+'\n' if content and title!=content else ''
            raw=prefix+content if content else title
            offset=e['start']+(len(prefix) if e['field']=='content' else 0)
            quote=e['quote']
            if raw[offset:offset+len(quote)]!=quote:
                continue
            cursor=0
            covered=offset
            ids=[]
            for paragraph in loads(src.paragraphs_json,[]):
                text=paragraph['text']
                start=raw.find(text,cursor)
                if start<0:
                    break
                cursor=start+len(text)
                if cursor<=offset or start>=offset+len(quote):
                    continue
                if start>covered and raw[covered:start].strip():
                    break
                covered=max(covered,cursor)
                ids.append(paragraph['id'])
            if ids and covered>=offset+len(quote):
                e['source_id'],e['paragraph_id'],e['paragraph_ids']=src.source_id,ids[0],ids
                cites.extend(src.source_id+'/'+pid for pid in ids)
        if section['evidence'] and not cites:
            section['gap']='未定位到冻结研究段落，须补查'
        lines.append(section['title'][:150]+' · '+(', '.join(dict.fromkeys(cites)) or '证据不足，请定向补查'))
    case.goal+='\n按以下大纲逐项核对引用段落后写作，区分计划、否认、已报道行动；缺证据的分节保留为待查。\n'+'\n'.join(lines)
    s.add(SurveyPacket(case_id=case.case_id,data=json.dumps(plan,ensure_ascii=False)))
