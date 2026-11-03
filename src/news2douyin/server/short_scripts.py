from pathlib import Path

from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlmodel import Session

from ..editorial import short_scripts as short
from ..research.models import ShortScriptBrief
from ..video import workbench as wb
from .research import call, same_origin


def register_short_script_routes(app, engine, storage_root):
    env = Environment(loader=FileSystemLoader(str(Path(__file__).parent / 'templates')),
                      autoescape=select_autoescape(['html']))

    def require(s, case_id):
        row = s.get(ShortScriptBrief, case_id)
        if not row:
            raise KeyError('短稿任务不存在')
        return row

    @app.get('/short-scripts', response_class=HTMLResponse)
    def page():
        return HTMLResponse(env.get_template('short_scripts.html').render(active='short_scripts', case_id=''))

    @app.get('/short-scripts/{case_id}', response_class=HTMLResponse)
    def detail_page(case_id: str):
        with Session(engine) as s:
            call(require, s, case_id)
        return HTMLResponse(env.get_template('short_scripts.html').render(active='short_scripts', case_id=case_id))

    @app.get('/api/short-scripts')
    def listing():
        return short.list_preparations(engine)

    @app.get('/api/short-scripts/{case_id}')
    def detail(case_id: str):
        with Session(engine) as s:
            return short.summary(s, call(require, s, case_id))

    @app.post('/api/short-scripts', status_code=202)
    def create(payload: short.PreparationRequest, request: Request):
        same_origin(request)
        # Persist the brief before submitting; retries recover the same case.
        case = call(short.create, engine, payload.model_dump())
        if not case['active_run_id'] and not case['short_script']['package_key']:
            call(app.state.research.enqueue, case['case_id'])
        return detail(case['case_id'])

    @app.post('/api/short-scripts/{case_id}/start', status_code=202)
    def retry(case_id: str, request: Request):
        same_origin(request)
        with Session(engine) as s:
            call(require, s, case_id)
        call(app.state.research.enqueue, case_id)
        return detail(case_id)

    @app.get('/api/scripts/{package_key}/images/{filename}')
    def image(package_key: str, filename: str):
        with Session(engine) as s:
            script = call(wb.script_detail, s, package_key)
        item = next((i for i in script.get('editorial', {}).get('image_candidates', [])
                     if i.get('filename') == filename and i.get('status') == 'downloaded'), None)
        if not item:
            raise HTTPException(404, '配图不存在')
        try:
            data = short.image_bytes(storage_root, item)
        except (FileNotFoundError, ValueError):
            raise HTTPException(410, '配图文件缺失或校验失败') from None
        return Response(data, media_type=item['mime_type'], headers={'X-Content-Type-Options': 'nosniff',
            'Content-Disposition': f'inline; filename="{filename}"', 'Cache-Control': 'private, max-age=3600'})
