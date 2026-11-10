"""HTTP views for the full archive intake; search is always after retention."""
from datetime import datetime, timezone
import hmac
import os
import re
from fastapi import Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select
from sqlalchemy import func
from ..intake import service
from ..intake.lake import Lake
from ..intake.projection import IntakeDocument, import_document, lookup_receipt, projection_status
from ..extraction.models import VersionReading
from .research import same_origin
from .tasks import task_call

FEEDS={'gkg_english':'GKG English','gkg_translation':'GKG Translingual'}


class FeedSetting(BaseModel):
    model_config=ConfigDict(extra='forbid')
    enabled:bool
    start_batch:str=Field(default='',max_length=32)


def stamp_value(value):
    value=value.strip()
    if not value:return ''
    try:
        if re.fullmatch(r'\d{14}',value):stamp=datetime.strptime(value,'%Y%m%d%H%M%S')
        else:
            stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
            if stamp.tzinfo:stamp=stamp.astimezone(timezone.utc)
        if stamp.minute not in (0,15,30,45) or stamp.second or stamp.microsecond:raise ValueError()
        return stamp.strftime('%Y%m%d%H%M%S')
    except ValueError:raise ValueError('开始批次须为 UTC 整刻，例如 2026-11-04T10:15')


def register(app,engine,storage,render):
    """Called by the app after intake models and services have been initialized."""
    lake=Lake(storage)

    def db():
        with Session(engine) as s:yield s

    def known(feed):
        if feed not in FEEDS:raise HTTPException(404,'采集流不存在')

    def connector_auth(authorization:str=Header(default='')):
        token=os.getenv('NEWS2DOUYIN_INTAKE_TOKEN','').strip()
        if not token:raise HTTPException(503,'外部文档接入尚未配置服务令牌')
        scheme,separator,provided=authorization.partition(' ')
        if (not separator or scheme.casefold()!='bearer' or
                not hmac.compare_digest(provided.encode('utf-8'),token.encode('utf-8'))):
            raise HTTPException(401,'外部文档接入令牌无效',headers={'WWW-Authenticate':'Bearer'})

    def readable_lake():
        worker=getattr(app.state,'intake',None)
        receiver=getattr(worker,'archive',worker)
        if receiver and getattr(receiver,'status_error',''):
            raise HTTPException(503,'此接收目录由其他服务进程持有，请从正在接收的主实例查看记录')

    def info():
        data=service.status(engine,storage)
        data['projection']=projection_status(engine)
        worker=getattr(app.state,'intake',None)
        receiver=getattr(worker,'archive',worker)
        data['receiver']=dict(running=bool(receiver and getattr(receiver,'running',False)),
            last_error=str(getattr(receiver,'status_error','') or getattr(receiver,'last_error','')
                or getattr(receiver,'startup_error','') or getattr(worker,'last_error','')) if receiver else '')
        with Session(engine) as s:
            data['extraction']=dict(s.exec(select(VersionReading.state,func.count()).group_by(VersionReading.state)).all())
        data['feed_labels']=FEEDS
        return data

    @app.get('/intake')
    def page():
        return render('intake.html',active='intake')

    @app.get('/api/intake/status')
    def status():
        return info()

    @app.get('/api/intake/records')
    def records(q:str=Query('',max_length=200),feed:str='',offset:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=100),s=Depends(db)):
        readable_lake()
        if feed:known(feed)
        data=task_call(lake.search,q=q,feed=feed,offset=offset,limit=limit)
        # Search summaries never return entire raw lines or large metadata blobs.
        rows=[]
        for item in data['items']:
            row={k:item.get(k) for k in ('feed','stamp','row_index','record_id','url','title','valid','error','document_date','observed_at')}
            row['projection']=lookup_receipt(s,item['feed'],item['stamp'],item['row_index'])
            row['content_available']=False
            rows.append(row)
        return dict(items=rows,total=data['total'],offset=offset,limit=limit,scope='仅筛选已经完整保存的接收记录，不影响后续采集')

    @app.get('/api/intake/records/{feed}/{stamp}/{row_index}')
    def record(feed:str,stamp:str,row_index:int,s=Depends(db)):
        readable_lake()
        known(feed)
        if row_index<1 or not re.fullmatch(r'\d{14}',stamp):raise HTTPException(422,'记录编号无效')
        items=task_call(lake.rows,feed,stamp,after=row_index-1,limit=1)
        if not items or items[0]['row_index']!=row_index:raise HTTPException(404,'接收记录不存在')
        item=dict(items[0]);raw=item.get('raw_line','')
        item['raw_line_chars']=len(raw);item['raw_line_truncated']=len(raw)>200000
        item['raw_line']=raw[:200000]
        item['projection']=lookup_receipt(s,feed,stamp,row_index)
        item['content_available']=False
        item['content_note']='GKG 提供报道元数据和链接，不含原始报道全文；全文可从已保存的新闻版本或来源页面核查。'
        return item

    @app.post('/api/intake/feeds/{feed}/enabled',dependencies=[Depends(same_origin)])
    def setting(feed:str,payload:FeedSetting):
        known(feed)
        task_call(service.configure,engine,feed,enabled=payload.enabled,start_batch=task_call(stamp_value,payload.start_batch))
        return info()

    @app.post('/api/intake/feeds/{feed}/retry',dependencies=[Depends(same_origin)])
    def retry(feed:str):
        known(feed);task_call(service.retry,engine,feed)
        return info()

    @app.post('/api/intake/documents',dependencies=[Depends(same_origin),Depends(connector_auth)])
    def document(payload:IntakeDocument):
        if payload.provider.casefold() in {'gkg','gdelt','worldnewsapi','mock'}:
            raise HTTPException(422,'请用应用编号作为采集来源，例如 industry.site；此入口不能声明为保留的官方采集渠道')
        # A connector's provider is a provenance claim, not verified publisher identity.
        return task_call(import_document,engine,payload.model_dump())
