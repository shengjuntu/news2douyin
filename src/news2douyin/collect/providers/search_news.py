"""WorldNewsAPI keyword search, shared by schedules and event research.

Contract: https://worldnewsapi.com/docs/search-news/ and /docs/authentication/
Uses x-api-key so credentials never become query parameters or saved provenance.
"""
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import requests

from ...search.dates import date_range
from ...storage.models import utc_now_iso

ENDPOINT = 'https://api.worldnewsapi.com/search-news'


def search_news(query, *, api_key_env='API_KEY'):
    from ...search.schemas import ResearchQuery
    query=ResearchQuery.model_validate(query).model_dump()
    api_key=os.getenv(api_key_env,'')
    if not api_key:
        raise ValueError('新闻源凭据未配置，请在服务端设置 '+api_key_env)
    start,end=date_range('custom',query.get('date_from',''),query.get('date_to',''),query.get('timezone','Asia/Shanghai'))
    params={'text':query['query'],'number':query.get('limit',20),'sort':'publish-time','sort-direction':'DESC'}
    if query.get('country'): params['source-countries']=query['country']
    if query.get('language'): params['language']=query['language']
    if start: params['earliest-publish-date']=datetime.fromisoformat(start).strftime('%Y-%m-%d %H:%M:%S')
    if end: params['latest-publish-date']=(datetime.fromisoformat(end)-timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M:%S')
    try:
        response=requests.get(ENDPOINT,params=params,headers={'x-api-key':api_key},timeout=(5,30),allow_redirects=False)
        if response.status_code != 200:
            code=response.status_code
            hint='请检查 API Key 和账户权限' if code in {401,403} else '请检查额度与调用频率' if code in {402,429} else '请检查查询条件或稍后重试'
            raise ValueError(f'新闻源返回 HTTP {code}；{hint}')
        data=response.json()
    except requests.Timeout:
        raise ValueError('新闻检索超时，请稍后重新检索') from None
    except requests.RequestException:
        raise ValueError('无法连接新闻源，请检查服务端网络') from None
    except (ValueError,TypeError) as exc:
        if isinstance(exc,ValueError) and str(exc).startswith('新闻源返回 HTTP'): raise
        raise ValueError('新闻源返回了无法解析的数据') from None
    if not isinstance(data,dict) or not isinstance(data.get('news'),list):
        raise ValueError('新闻源返回格式无效：缺少 news 列表')
    items=[]
    for raw in data['news'][:query.get('limit',20)]:
        if not isinstance(raw,dict): continue
        url=str(raw.get('url') or '')
        try:
            parsed=urlsplit(url)
            if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password: continue
        except ValueError: continue
        title=str(raw.get('title') or '').strip()
        if not title: continue
        content=str(raw.get('text') or raw.get('summary') or '')
        published=str(raw.get('publish_date') or '')
        try:
            stamp=datetime.fromisoformat(published.replace('Z','+00:00'))
            if stamp.tzinfo is None: stamp=stamp.replace(tzinfo=timezone.utc)
            published=stamp.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
        except ValueError: published=''
        item={'title':title[:1000],'content':content[:50000],'url':url,'source':{'domain':parsed.hostname},
              'published_at':published,'fetched_at':utc_now_iso(),'provider':'worldnewsapi',
              'country':str(raw.get('source_country') or query.get('country') or ''),
              'language':str(raw.get('language') or query.get('language') or ''),
              'research_meta':{'provider':'worldnewsapi','endpoint':ENDPOINT,'upstream_id':raw.get('id'),
                    'content_kind':'text' if raw.get('text') else 'summary','content_truncated':len(content)>50000}}
        items.append(item)
    available=data.get('available',len(items))
    return {'items':items,'available':available if type(available) is int and available>=0 else len(items)}
