"""History archive management and targeted requests from investigations."""
from datetime import date, datetime, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

from .research import same_origin


PAGE = '/admin/history-archive'
API = '/api/history-archive'
FIRST_DAY = date(1979, 1, 1)
CaseKey = Annotated[str, StringConstraints(pattern=r'^ci_[0-9a-f]{32}$')]
NodeKey = Annotated[str, StringConstraints(pattern=r'^cn_[0-9a-f]{32}$')]


class DownloadRange(BaseModel):
    model_config = ConfigDict(extra='forbid')

    date_from: date
    date_to: date
    mentions: bool = False

    @model_validator(mode='after')
    def check_dates(self):
        if not FIRST_DAY <= self.date_from <= self.date_to <= datetime.now(timezone.utc).date():
            raise ValueError('日期范围须介于 1979-01-01 和今日（UTC）之间，起始日期不能晚于结束日期')
        return self


class WindowRequest(DownloadRange):
    case_key: CaseKey
    node_key: NodeKey


def _reply(value, status=200):
    return JSONResponse(jsonable_encoder(value), status_code=status, headers={'Cache-Control': 'no-store'})


def register_history_archive(app, render):
    def worker():
        service = getattr(app.state, 'history_archive', None)
        if service is None:
            raise HTTPException(503, '历史归档服务暂不可用')
        return service

    def update(action, payload=None, status=202):
        runtime = worker()
        args = payload.model_dump() if payload is not None else {}
        try:
            result = getattr(runtime, action)(**args)
        except KeyError:
            raise HTTPException(404, '调查或节点不存在') from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from None
        return _reply(result if isinstance(result, dict) else {'ok': True}, status)

    @app.get(PAGE, response_class=HTMLResponse)
    def page():
        return render('history_archive.html', active='history-archive', first_date=FIRST_DAY.isoformat(),
                      today=datetime.now(timezone.utc).date().isoformat())

    @app.get(API)
    def status(limit: Annotated[int, Query(ge=1, le=120)] = 30,
               offset: Annotated[int, Query(ge=0, le=5_000_000)] = 0):
        return _reply(worker().status(coverage_limit=limit, coverage_offset=offset))

    @app.post(API + '/start', status_code=202, dependencies=[Depends(same_origin)])
    def start(payload: DownloadRange):
        return update('start', payload)

    @app.post(API + '/pause', status_code=202, dependencies=[Depends(same_origin)])
    def pause():
        return update('pause')

    @app.post(API + '/resume', status_code=202, dependencies=[Depends(same_origin)])
    def resume():
        return update('resume')

    @app.post(API + '/retry', status_code=202, dependencies=[Depends(same_origin)])
    def retry():
        return update('retry')

    @app.post(API + '/request', status_code=202, dependencies=[Depends(same_origin)])
    def request_window(payload: WindowRequest):
        return update('request_window', payload)
