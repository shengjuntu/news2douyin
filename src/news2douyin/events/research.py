"""Persist searches before network I/O; import only user-selected candidates."""
from uuid import uuid4
from sqlmodel import Session, select

from ..collect.providers.search_news import search_news
from ..dedup.service import decide_duplicate
from ..enrich.service import enrich_item
from ..storage.articles import lock_news, find_article, identity_key, record_version, latest_version
from ..storage.models import Article, ArticleIdentity, EventResearch, utc_now_iso
from ..storage.utils import loads,dumps
from ..tasks.control import TaskConflict
from .workbench import require_event,lock_event,_add_sources,audit,finish,workspace


def result(session,key,research_key):
    row=session.get(EventResearch,research_key)
    if not row or row.event_key != key: raise KeyError('检索记录不存在')
    return dict(research_key=row.research_key,event_key=key,status=row.status,query=loads(row.query_json,{}),
        results=loads(row.results_json,[]),available=row.available,imported=loads(row.imported_json,{}),
        created_at=row.created_at,finished_at=row.finished_at,error=row.error_text)


def search(engine,key,query):
    research_key='res_'+uuid4().hex[:20]
    with Session(engine) as session:
        lock_news(session)
        require_event(session,key,active=True)
        session.add(EventResearch(research_key=research_key,event_key=key,query_json=dumps(query)))
        session.commit()
    try:
        response=search_news(query)
        items=response['items']
        error=''
    except ValueError as exc:
        items,response,error=[],{},str(exc)
    except Exception:
        # Provider errors can contain credentials; persist only a stable message.
        items,response,error=[],{},'检索未完成，请检查服务连接后重新检索'
    with Session(engine) as session:
        lock_news(session)
        row=session.get(EventResearch,research_key)
        row.results_json=dumps(items)
        row.available=response.get('available',len(items))
        row.status='failed' if error else 'succeeded'
        row.error_text=error
        row.finished_at=utc_now_iso()
        session.add(row)
        session.commit()
        return result(session,key,research_key)


def import_results(session,key,research_key,data):
    state=lock_event(session,key,data['expected_version'])
    row=session.get(EventResearch,research_key)
    if not row or row.event_key != key: raise KeyError('检索记录不存在')
    if row.status != 'succeeded': raise TaskConflict('只能收录已完成检索的结果')
    items,imported=loads(row.results_json,[]),loads(row.imported_json,{})
    indexes=list(dict.fromkeys(data['indexes']))
    if any(i<0 or i>=len(items) for i in indexes): raise ValueError('检索结果编号无效')
    keys,changes=[],[]
    for index in indexes:
        item=dict(items[index])
        existing=find_article(session,item)
        if not existing:
            # Be tolerant of legacy rows created without the identity index.
            from ..dedup.normalize import canonicalize_url
            canonical=canonicalize_url(item['url'])
            existing=session.exec(select(Article).where(Article.canonical_url==canonical).order_by(Article.id)).first() if canonical else None
        if existing:
            from .workbench import ensure_version
            version=ensure_version(session,existing)
            article=existing
            disposition='existing_preserved'
        else:
            item=enrich_item(item,{'country':item.get('country',''),'categories':[]})
            decision=decide_duplicate(session,item)
            norm=decision.normalized
            article=Article(article_key=identity_key(item)[:24],provider='worldnewsapi',url=item['url'],canonical_url=norm.canonical_url,
                title=item['title'],content=item['content'],source_domain=item['source']['domain'],published_at=item.get('published_at',''),
                fetched_at=item.get('fetched_at',utc_now_iso()),country=item.get('country',''),language=item.get('language',''),raw_json=dumps(item),
                normalized_title=norm.normalized_title,normalized_content=norm.normalized_content,title_hash=norm.title_hash,
                content_hash=norm.content_hash,title_signature=norm.title_signature,content_signature=norm.content_signature,
                category_tags_json=dumps(item.get('category_tags',[])),sentiment=item.get('sentiment','neutral'),
                market_relevance_score=item.get('market_relevance_score',0),is_duplicate=decision.is_duplicate,
                duplicate_of_article_key=decision.duplicate_of_article_key,dedup_group_id=decision.dedup_group_id,
                dedup_reason=decision.reason,dedup_score=decision.score)
            session.add(article)
            session.flush()
            record_version(session,article,origin='external_research',run_key=research_key)
            session.flush()
            version=latest_version(session,article.article_key)
            disposition='created'
        identity=identity_key(item)
        if not session.get(ArticleIdentity,identity):
            session.add(ArticleIdentity(identity_key=identity,article_key=article.article_key))
        keys.append(article.article_key)
        saved=dict(article_key=article.article_key,revision=version.revision,content_hash=version.content_hash,disposition=disposition)
        if str(index) not in imported:
            imported[str(index)]=saved
            changes.append(dict(index=index,**saved))
    added=_add_sources(session,key,state,keys,'外部检索 '+research_key,'research')
    row.imported_json=dumps(imported)
    session.add(row)
    if changes or added:
        audit(session,key,'import_research',{'research_key':research_key,'query':loads(row.query_json,{}),'results':changes,'added':added})
        return finish(session,key,state)
    session.commit()
    return workspace(session,key)
