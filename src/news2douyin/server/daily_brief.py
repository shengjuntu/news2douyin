"""Download the exact published edition selected by a reader."""
from html import escape

from fastapi import HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from sqlmodel import Session

from ..digest.brief_export import markdown_for_issue, markdown_for_shortlist
from .daily_radar import issue_data


def _download(engine, day, revision, render, filename_prefix):
    with Session(engine) as session:
        issue = issue_data(session, day)
        if issue['status'] != 'ready':
            raise HTTPException(404, '该日日报尚未发布，请先在每日精选中查看生成进度。')
        if revision is not None and issue['revision'] != revision:
            return HTMLResponse(
                '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>日报已更新</title>'
                '<h1>日报已更新</h1><p>你打开的版本已经被替换。请返回最新版本再下载。</p>'
                f'<p><a href="/radar?day={escape(issue["day"])}">返回这期日报</a></p></html>',
                status_code=409, headers={'Cache-Control': 'no-store'})
        markdown = render(session, issue)
    name = f'news2douyin-{filename_prefix}-{issue["day"]}-r{issue["revision"]}.md'
    return Response(markdown, media_type='text/markdown; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="{name}"',
                             'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})


def register(app, engine):
    @app.get('/api/radar/daily/{day}/shortlist.md', response_class=Response)
    def daily_shortlist(day: str, revision: int | None = Query(None, ge=1)):
        return _download(engine, day, revision, markdown_for_shortlist, 'shortlist')

    @app.get('/api/radar/daily/{day}/brief.md', response_class=Response)
    def daily_brief(day: str, revision: int | None = Query(None, ge=1)):
        return _download(engine, day, revision, markdown_for_issue, 'brief')
