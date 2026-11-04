"""An explicit operator link to the separately served FalkorDB Browser."""
import os
from urllib.parse import urlsplit
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select
from ..desk.models import DeskMeta, Watch
from ..storage.articles import lock_news
from .research import same_origin


def browser_url(raw):
    value=(raw or '').strip()
    if not value:return ''
    try:
        url=urlsplit(value)
        if (len(value)>2000 or any(ord(c)<33 for c in value) or '\\' in value
                or url.scheme not in ('http','https') or not url.hostname
                or url.username is not None or url.password is not None):raise ValueError()
        url.port
    except ValueError:
        raise ValueError('请填写 http 或 https 网页地址，不包含账号和密码')
    return value


class BrowserSetting(BaseModel):
    url:str=Field(default='',max_length=2000)


def register(app,engine,render):
    def db():
        with Session(engine) as s:yield s

    @app.get('/knowledge/browser')
    def page(s=Depends(db)):
        saved=s.get(DeskMeta,'graph_browser_url')
        raw=saved.value if saved else os.getenv('NEWS2DOUYIN_FALKOR_BROWSER_URL','')
        try:url=browser_url(raw)
        except ValueError:url=''
        prefix=app.state.desk.graph.prefix
        graphs=[dict(name=prefix+'_active_global',label='全局活动知识图')]
        graphs.extend(dict(name=prefix+'_focus_'+w.key,label=w.name) for w in s.exec(select(Watch).order_by(Watch.name)))
        return render('graph_browser.html',active='knowledge',browser_url=url,graphs=graphs)

    @app.post('/api/knowledge/browser-setting',dependencies=[Depends(same_origin)])
    def save(payload:BrowserSetting,s=Depends(db)):
        try:url=browser_url(payload.url)
        except ValueError as exc:raise HTTPException(422,str(exc))
        lock_news(s);s.merge(DeskMeta(key='graph_browser_url',value=url));s.commit()
        return dict(url=url)
