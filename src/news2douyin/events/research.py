"""Persist searches before network I/O; import only user-selected candidates."""
from uuid import uuid4
from sqlmodel import Session

from ..candidates.promotion import attach_articles, store_article
from ..collect.providers.search_news import search_news
from ..storage.articles import lock_news, identity_key
from ..storage.models import EventResearch, utc_now_iso
from ..storage.utils import loads,dumps
from ..tasks.control import TaskConflict
from .workbench import require_event,lock_event,audit,finish,workspace


def result(session,key,research_key):
    row=session.get(EventResearch,research_key)
    if not row or row.event_key != key: raise KeyError('检索记录不存在')
    return dict(research_key=row.research_key,event_key=key,status=row.status,query=loads(row.query_json,{}),
        results=loads(row.results_json,[]),available=row.available,imported=loads(row.imported_json,{}),
        created_at=row.created_at,finished_at=row.finished_at,error=row.error_text)


def search(engine,key,query):
    research_key='res_'+uuid4().hex[:20]
    with Session(engine) as session:
        lock_news(session, 'event:' + key)
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
        lock_news(session, 'event-research:' + research_key)
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
    row=session.get(EventResearch,research_key)
    if not row or row.event_key != key: raise KeyError('检索记录不存在')
    if row.status != 'succeeded': raise TaskConflict('只能收录已完成检索的结果')
    items,imported=loads(row.results_json,[]),loads(row.imported_json,{})
    indexes=list(dict.fromkeys(data['indexes']))
    if any(i<0 or i>=len(items) for i in indexes): raise ValueError('检索结果编号无效')
    # Same article-before-event lock order as the collection and promotion paths.
    lock_news(session, *('article:' + identity_key(items[i]) for i in indexes))
    state=lock_event(session,key,data['expected_version'])
    imported=loads(row.imported_json,{})
    keys,changes=[],[]
    references={}
    for index in indexes:
        item=dict(items[index], provider='worldnewsapi')
        kind=(item.get('research_meta') or {}).get('content_kind') or 'text'
        item['content_kind']=kind
        source_ref=dict(research_key=research_key,result_index=index)
        source=dict(provider='worldnewsapi', source_ref=source_ref, content_kind=kind,
                    requires_original_text=kind != 'text')
        if kind!='text':
            source['excerpt']=item.get('content','')[:800]
            item.update(content='',material_kind='candidate_lead',requires_original_text=True)
        article,version,disposition=store_article(session,item,origin='external_research',run_key=research_key)
        references.setdefault(article.article_key,source)
        keys.append(article.article_key)
        saved=dict(article_key=article.article_key,revision=version.revision,content_hash=version.content_hash,disposition=disposition)
        if str(index) not in imported:
            imported[str(index)]=saved
            changes.append(dict(index=index,**saved))
    added=attach_articles(session,state,keys,note='外部检索 '+research_key,origin='research',sources=references)
    row.imported_json=dumps(imported)
    session.add(row)
    if changes or added:
        audit(session,key,'import_research',{'research_key':research_key,'query':loads(row.query_json,{}),'results':changes,'added':added})
        return finish(session,key,state)
    session.commit()
    return workspace(session,key)
