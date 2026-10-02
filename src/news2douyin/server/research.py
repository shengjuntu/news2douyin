"""Research UI/API and stateless MCP Streamable HTTP endpoint."""
import hmac
import json
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
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


def same_origin(request):
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
        return page('research.html', article_key=article_key, event_key=event_key)

    @app.get('/research/{case_id}', response_class=HTMLResponse)
    def research_case(case_id: str):
        with Session(engine, expire_on_commit=False) as s:
            call(svc.require_case, s, case_id)
        return page('research_case.html', case_id=case_id)

    @app.get('/api/research/settings')
    def read_settings():
        return settings.public()

    def ensure_idle():
        with Session(engine, expire_on_commit=False) as s:
            if s.exec(select(ResearchRun).where(ResearchRun.status.in_(svc.ACTIVE))).first():
                raise ValueError('有活动研究任务，请先停止后再修改研究环境')

    @app.put('/api/research/settings')
    async def update_settings(request: Request):
        same_origin(request)
        payload = await body(request)
        call(ensure_idle)
        return call(settings.update, payload)

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
            for row in s.exec(select(ResearchCase).order_by(ResearchCase.updated_at.desc()).limit(100)):
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
                      'serverInfo': {'name': 'news2douyin-research', 'version': '0.11.1'}}
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
