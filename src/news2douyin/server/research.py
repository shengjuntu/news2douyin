"""Research UI/API and stateless MCP Streamable HTTP endpoint."""
import os
from contextlib import contextmanager
import hmac
import json
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Form, Query, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlmodel import Session, select
from starlette.concurrency import run_in_threadpool
from starlette.staticfiles import StaticFiles

from ..research.config import SettingsStore
from ..research.models import ResearchCase, ResearchRun, ResearchSource
from ..research import service as svc
from ..research.runtime import ResearchRuntime
from ..research.rundesk import bootstrap, connection_check
from ..research.tools import ResearchTools, tool_list


def call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KeyError:
        raise HTTPException(404, '研究资源不存在') from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)[:1200]) from None


def same_origin(request: Request):
    origin = request.headers.get('origin')
    if origin:
        parsed = urlsplit(origin)
        if parsed.scheme != request.url.scheme or parsed.netloc != request.url.netloc:
            raise HTTPException(403, '跨来源请求被拒绝')


async def body(request, maximum=240000):
    size = 0
    chunks = []
    async for part in request.stream():
        size += len(part)
        if size > maximum:
            raise HTTPException(413, '请求内容过大')
        chunks.append(part)
    try:
        value = json.loads(b''.join(chunks))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, '需要 JSON 对象') from None


def register_research_routes(app, engine, storage_root):
    app.mount('/research-assets', StaticFiles(directory=Path(__file__).parent / 'static'), name='research-assets')
    settings = SettingsStore(storage_root)
    runtime = ResearchRuntime(engine, settings)
    tools = ResearchTools(engine, settings)
    app.state.research = runtime
    app.state.research_settings = settings
    env = Environment(loader=FileSystemLoader(str(Path(__file__).parent / 'templates')), autoescape=select_autoescape(['html']))

    def page(name, **context):
        return HTMLResponse(env.get_template(name).render(active='research', **context))

    @app.get('/research/settings', response_class=HTMLResponse)
    def settings_page():
        return page('research_settings.html')

    @app.get('/research', response_class=HTMLResponse)
    def research_page(article_key: str = '', event_key: str = ''):
        from urllib.parse import urlencode
        if article_key or event_key:
            return RedirectResponse('/workbench/new?'+urlencode(dict(article=article_key,event=event_key)),303)
        return RedirectResponse('/workbench?view=history',303)

    @app.get('/research/market-impact', response_class=HTMLResponse)
    def market_impact_page(event_key: str = Query('', max_length=160),
                           candidate_key: str = Query('', max_length=240),
                           moment_key: str = Query('', max_length=160)):
        from ..research.market_impact import context

        with Session(engine) as s:
            selection = call(context, s, storage_root, event_key=event_key,
                             candidate_key=candidate_key, moment_key=moment_key)
        return page('market_impact.html', selection=selection)

    @app.post('/research/market-impact')
    def start_market_impact(request: Request, receipt: str = Form(..., pattern=r'^[a-f0-9]{64}$'),
                            event_key: str = Form('', max_length=160),
                            candidate_key: str = Form('', max_length=240),
                            moment_key: str = Form('', max_length=160)):
        from ..research.market_impact import context, create_case

        same_origin(request)
        with Session(engine) as s:
            selection = call(context, s, storage_root, event_key=event_key,
                             candidate_key=candidate_key, moment_key=moment_key)
        if selection['receipt'] != receipt:
            raise HTTPException(409, '选中的报道或事件已更新，请返回研究入口核对，再发起研究')
        case = call(create_case, engine, selection)
        case_id = case['case_id']
        request_run = 'rrun_' + svc.digest(['market-impact', case_id])[:24]
        try:
            if not any(run['run_id'] == request_run for run in case['runs']):
                runtime.enqueue(case_id, request_run_id=request_run)
        except ValueError as exc:
            return page('market_impact.html', selection=selection, error=str(exc), case_id=case_id)
        return RedirectResponse('/research/' + case_id, 303)

    @app.get('/research/{case_id}', response_class=HTMLResponse)
    def research_case(case_id: str):
        from .workbench import owner
        with Session(engine, expire_on_commit=False) as s:
            case = call(svc.require_case, s, case_id)
            task=owner(s,'case',case_id)
            if task:return RedirectResponse(f'/workbench/{task}/research?case={case_id}&mode=evidence',303)
            from ..desk.models import WatchReview, Watch
            from ..extraction.models import SurveyPacket
            packet = s.get(SurveyPacket,case_id)
            plan = json.loads(packet.data) if packet else None
            continuous_key = (json.loads(case.strategy_json).get('focus_watch') or {}).get('watch')
            if continuous_key:
                watch = s.get(Watch, continuous_key)
            else:
                watch = s.exec(select(Watch).join(WatchReview, WatchReview.watch == Watch.key)
                               .where(WatchReview.case_key == case_id)).first()
            from ..desk.focus_lifecycle import is_deleted
            if watch and is_deleted(s,watch.key):watch=None
        return page('research_case.html', case_id=case_id, watch=watch, continuous_case=bool(continuous_key),
                    event_key=case.event_key, plan=plan)

    @app.get('/api/research/settings')
    def read_settings():
        return settings.public()

    def ensure_idle():
        with Session(engine, expire_on_commit=False) as s:
            if s.exec(select(ResearchRun).where(ResearchRun.status.in_(svc.ACTIVE))).first():
                raise ValueError('有活动研究任务，请先停止后再修改研究环境')

    @contextmanager
    def guard_environment(previous, incoming):
        from ..desk.focus_watch_models import FocusBotBinding
        from ..storage.articles import lock_news
        with Session(engine) as session:
            lock_news(session, 'research-host-binding')
            bound = session.exec(select(FocusBotBinding.watch).where(FocusBotBinding.owned.is_(True),
                                  FocusBotBinding.host_url != incoming['rundesk_url']).limit(1)).first()
            if bound:
                raise ValueError('请先将焦点 ' + bound + ' 的持续研究交回本地，等候 RunDesk 确认后再更换地址。'
                                 '若焦点在回收站中，请先恢复。')
            changed = {k for k, value in incoming.items() if previous.get(k) != value}
            if changed != {'rundesk_token'}:
                if session.exec(select(ResearchRun.run_id).where(ResearchRun.status.in_(svc.ACTIVE)).limit(1)).first():
                    raise ValueError('有活动研究任务，请先停止后再修改研究环境')
            yield
            session.commit()

    @app.put('/api/research/settings')
    async def update_settings(request: Request):
        same_origin(request)
        payload = await body(request)
        if not payload.get('callback_url') and set(payload) <= {'rundesk_url', 'rundesk_token'} and payload.get('rundesk_token'):
            current = settings.get()
            payload.setdefault('rundesk_url', current['rundesk_url'])
            payload['callback_url'] = current['callback_url']
        payload['callback_url'] = payload.get('callback_url') or os.getenv('NEWS2DOUYIN_PUBLIC_URL') or str(request.base_url).rstrip('/')
        return await run_in_threadpool(call, settings.update, payload, guard=guard_environment)

    @app.post('/api/research/setup')
    def setup_research(request: Request):
        same_origin(request)
        call(ensure_idle)
        return call(bootstrap, settings, runtime.client_factory)

    @app.post('/api/research/check')
    def check_research(request: Request):
        same_origin(request)
        return call(connection_check, settings, runtime.client_factory)

    @app.get('/api/research/strategies')
    def get_strategies():
        with Session(engine, expire_on_commit=False) as s:
            return svc.strategies(s)

    @app.put('/api/research/strategies/{strategy_id}')
    async def put_strategy(strategy_id: str, request: Request):
        same_origin(request)
        payload = await body(request)
        return call(svc.save_strategy, engine, strategy_id, payload)

    @app.get('/api/research/cases')
    def get_cases():
        with Session(engine, expire_on_commit=False) as s:
            result = []
            from ..topics.models import TopicWorkspace
            for row in s.exec(select(ResearchCase).where(~ResearchCase.case_id.in_(select(TopicWorkspace.case_id))).order_by(ResearchCase.updated_at.desc()).limit(100)):
                run = s.get(ResearchRun, row.active_run_id)
                result.append(dict(case_id=row.case_id, title=row.title, goal=row.goal, updated_at=row.updated_at,
                                   status=run.status if run else 'draft'))
            return result

    @app.post('/api/research/cases')
    async def create_research(request: Request):
        same_origin(request)
        payload = await body(request)
        return call(svc.create_case, engine, payload)

    @app.get('/api/research/cases/{case_id}')
    def get_case(case_id: str):
        with Session(engine, expire_on_commit=False) as s:
            return call(svc.detail, s, case_id)

    @app.post('/api/research/cases/{case_id}/start')
    async def start_case(case_id: str, request: Request):
        same_origin(request)
        data = await body(request)
        return call(runtime.enqueue, case_id, data.get('instruction', ''), data.get('refresh_cutoff', False))

    @app.post('/api/research/cases/{case_id}/stop')
    def stop_case(case_id: str, request: Request):
        same_origin(request)
        return call(runtime.cancel, case_id)

    @app.post('/api/research/cases/{case_id}/sync')
    def sync_case(case_id: str, request: Request):
        same_origin(request)
        with Session(engine, expire_on_commit=False) as s:
            case = call(svc.require_case, s, case_id)
        return call(runtime.sync, case.active_run_id) if case.active_run_id else {'status': 'draft'}

    @app.post('/api/research/cases/{case_id}/steer')
    async def steer_case(case_id: str, request: Request):
        same_origin(request)
        data = await body(request)
        return await run_in_threadpool(call, runtime.steer, case_id, data.get('text'), data.get('request_id'))

    @app.get('/api/research/cases/{case_id}/sources/{source_id}')
    def read_source(case_id: str, source_id: str):
        with Session(engine, expire_on_commit=False) as s:
            row = s.get(ResearchSource, source_id)
            if not row or row.case_id != case_id:
                raise HTTPException(404, '资料不存在')
            return svc.source_dict(row, full=True)

    @app.get('/api/research/cases/{case_id}/reports/{report_id}/download')
    def download_report(case_id: str, report_id: str):
        with Session(engine, expire_on_commit=False) as s:
            text = call(svc.export_report, s, case_id, report_id)
        return Response(text, media_type='text/markdown; charset=utf-8',
                        headers={'Content-Disposition': 'attachment; filename="research-report.md"'})

    def authorize_mcp(request):
        same_origin(request)
        value = request.headers.get('authorization', '')
        if not hmac.compare_digest(value, 'Bearer ' + settings.get()['mcp_token']):
            raise HTTPException(401, 'MCP 认证失败', headers={'WWW-Authenticate': 'Bearer'})

    @app.get('/mcp/research')
    @app.delete('/mcp/research')
    def mcp_no_stream(request: Request):
        authorize_mcp(request)
        return Response(status_code=405, headers={'Allow': 'POST'})

    @app.post('/mcp/research')
    async def mcp_rpc(request: Request):
        authorize_mcp(request)
        value = await body(request)
        rid, method = value.get('id'), value.get('method')
        if value.get('jsonrpc') != '2.0' or not isinstance(method, str):
            return JSONResponse({'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32600, 'message': 'Invalid Request'}}, status_code=400)
        if 'id' not in value:
            return Response(status_code=202)
        params = value.get('params') or {}
        if not isinstance(params, dict):
            return JSONResponse({'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32602, 'message': 'Invalid params'}})
        if method == 'initialize':
            # Stateless subset: JSON responses, no server-to-client notifications.
            supported = {'2025-03-26', '2025-06-18', '2025-11-25'}
            version = params.get('protocolVersion')
            result = {'protocolVersion': version if version in supported else '2025-03-26',
                      'capabilities': {'tools': {'listChanged': False}},
                      'serverInfo': {'name': 'news2douyin-research', 'version': '0.65.0'}}
        elif method == 'ping':
            result = {}
        elif method == 'tools/list':
            result = {'tools': tool_list()}
        elif method == 'tools/call':
            try:
                data = await run_in_threadpool(tools.call, params.get('name'), params.get('arguments', {}))
                result = {'content': [{'type': 'text', 'text': svc.dump(data)}], 'isError': False}
            except (ValueError, KeyError, TypeError) as exc:
                result = {'content': [{'type': 'text', 'text': str(exc)[:1200]}], 'isError': True}
            except Exception:
                result = {'content': [{'type': 'text', 'text': '研究工具执行失败，未确认保存；请检查服务状态后继续'}], 'isError': True}
        else:
            return JSONResponse({'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32601, 'message': 'Method not found'}})
        return JSONResponse({'jsonrpc': '2.0', 'id': rid, 'result': result})
