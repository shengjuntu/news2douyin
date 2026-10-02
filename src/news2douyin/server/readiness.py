"""First-run checks. Reading configuration never starts a collection or a task."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from sqlmodel import Session, select

from ..collect.service import row_to_profile
from ..llm.settings import get_settings
from ..scheduler.cron import next_occurrences
from ..search.dates import calendar_day, timezone_name
from ..storage.models import CollectJob, CollectProfile, ProfileState, utc_now_iso


def model_configuration():
    settings = get_settings()
    valid, origin = True, ''
    try:
        url = urlsplit(settings.base_url)
        if (url.scheme not in {'http', 'https'} or not url.hostname or url.username is not None
                or url.password is not None or url.query or url.fragment
                or any(c.isspace() for c in settings.base_url)):
            raise ValueError()
        host = '[' + url.hostname + ']' if ':' in url.hostname else url.hostname
        origin = f'{url.scheme}://{host}' + (f':{url.port}' if url.port is not None else '')
    except ValueError:
        valid = False
    return dict(valid=valid and bool(settings.model.strip()), origin=origin,
                model=settings.model, key_present=bool(settings.api_key.strip()),
                defaults=[name for name in ('OPENAI_BASE_URL', 'OPENAI_API_KEY', 'MODEL') if name not in os.environ])


def _profile(row, enabled):
    provider = row.provider
    try:
        config = row_to_profile(row)
    except (ValueError, TypeError, AttributeError):
        return dict(name=row.name, enabled=enabled, provider=provider, state='invalid',
                    note='策略附加配置无法读取，请编辑并重新保存。')
    env_name = config.get('api_key_env', 'API_KEY')
    if provider == 'mock':
        state, note = 'demo', '演示资料，仅用于跑通操作。'
    elif provider != 'worldnewsapi':
        state, note = 'invalid', '当前服务不支持此新闻源，请编辑策略。'
    elif not isinstance(env_name, str) or not env_name.strip() or '=' in env_name or '\x00' in env_name:
        state, note = 'invalid', '密钥环境变量名无效，请在策略中填写 api_key_env，通常为 API_KEY。'
    elif not os.getenv(env_name, '').strip():
        state, note = 'missing', f'未配置 {env_name}；填写后重启服务。'
    elif config.get('collection_mode') == 'search' and not str(config.get('search_query', '')).strip():
        state, note = 'invalid', '主动检索缺少检索词，请编辑策略。'
    else:
        state, note = 'configured', '密钥已配置，尚需试跑验证账户权限与结果。'
    return dict(name=row.name, enabled=enabled, provider=provider, state=state, note=note)


def configuration_report(app):
    """Only local reads; do not expose credentials, database URLs or raw errors."""
    result = dict(checked_at=utc_now_iso(), database_ok=False, profiles=[], schedules=[],
                  model=model_configuration(), worker_running=bool(app.state.worker.running),
                  scheduler_running=bool(app.state.scheduler._thread and app.state.scheduler._thread.is_alive()))
    try:
        zone = timezone_name()
        result['calendar'] = dict(ok=True, timezone=zone, today=calendar_day(timezone=zone))
    except ValueError:
        result['calendar'] = dict(ok=False, timezone='', today='')
    try:
        root = Path(app.state.storage_root)
        result['storage'] = dict(exists=root.is_dir(), permissions_ok=root.is_dir() and os.access(root, os.W_OK | os.X_OK))
    except (OSError, ValueError):
        result['storage'] = dict(exists=False, permissions_ok=False)
    with Session(app.state.engine) as session:
        # A read-only snapshot also avoids obtaining the application write lock.
        if app.state.engine.dialect.name == 'sqlite':
            session.connection().exec_driver_sql('BEGIN')
        disabled = set(session.exec(select(ProfileState.name).where(ProfileState.enabled == False)))
        profiles = [_profile(row, row.name not in disabled) for row in session.exec(select(CollectProfile).order_by(CollectProfile.name))]
        by_name = {p['name']: p for p in profiles}
        schedules = []
        for job in session.exec(select(CollectJob).where(CollectJob.schedule_type != 'archived').order_by(CollectJob.id)):
            issues, next_run = [], ''
            profile = by_name.get(job.profile_name)
            if not profile:
                issues.append('关联策略不存在')
            elif not profile['enabled']:
                issues.append('关联策略已停用')
            elif profile['state'] in {'missing', 'invalid'}:
                issues.append('关联策略缺少必要配置')
            try:
                times = next_occurrences(job.cron_expr, job.timezone, count=1)
                next_run = times[0] if times else ''
                if not times:
                    issues.append('未来五年没有匹配的执行日期')
            except (ValueError, TypeError):
                issues.append('计划时间或时区无效')
            schedules.append(dict(id=job.id, name=job.name, enabled=job.enabled,
                                  profile_name=job.profile_name, timezone=job.timezone,
                                  next_run=next_run if job.enabled and not issues else '', issues=issues))
        result.update(database_ok=True, profiles=profiles, schedules=schedules)
    result['summary'] = dict(enabled_profiles=sum(p['enabled'] for p in profiles),
                            configured_news=sum(p['enabled'] and p['state'] == 'configured' for p in profiles),
                            demo_profiles=sum(p['enabled'] and p['state'] == 'demo' for p in profiles),
                            enabled_schedules=sum(j['enabled'] for j in schedules),
                            schedule_issues=sum(j['enabled'] and bool(j['issues']) for j in schedules))
    return result


class _NoRedirect(HTTPRedirectHandler):
    # Never forward a configured Authorization header to a redirected host.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe_model():
    """Explicit GET /models only. Catalog visibility does not prove generation."""
    config = model_configuration()
    def reply(state, message):
        return dict(state=state, message=message, checked_at=utc_now_iso(), configuration=config)
    if not config['valid']:
        return reply('invalid', '模型名称不能为空；服务地址须为 HTTP(S)，且不能包含账号、密码、查询参数或片段。')
    settings = get_settings()
    try:
        request = Request(settings.base_url + '/models', headers=settings.headers)
        with build_opener(_NoRedirect()).open(request, timeout=3) as response:
            if not 200 <= response.status < 300:
                return reply('http_error', '模型服务未返回成功状态，请检查兼容接口。')
            data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            return reply('invalid_response', '模型列表响应过大，无法核对。')
        payload = json.loads(data)
        if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
            return reply('invalid_response', '服务已响应，但不是兼容的模型列表。')
        names = {m.get('id') for m in payload['data'] if isinstance(m, dict) and isinstance(m.get('id'), str)}
        if settings.model not in names:
            return reply('model_not_listed', '模型列表已响应，但未列出当前 MODEL；请核对名称，部分服务也可能不公开完整列表。')
        return reply('listed', '模型列表已响应，并包含当前 MODEL。尚未验证生成能力，请提交一条 AI 初稿验证。')
    except HTTPError as exc:
        code = exc.code
        exc.close()
        if code in {401, 403}:
            return reply('auth_error', '服务拒绝鉴权，请检查 OPENAI_API_KEY 和访问权限。')
        if 300 <= code < 400:
            return reply('redirect', '服务返回重定向；检查 OPENAI_BASE_URL，填写最终兼容接口地址。')
        if code == 404:
            return reply('not_found', '未找到 /models；检查接口前缀，部分兼容服务可能不提供模型列表。')
        if code == 429:
            return reply('rate_limited', '服务限流或额度不足，请检查服务状态后再试。')
        return reply('http_error', f'模型列表请求失败（HTTP {code}）。')
    except (TimeoutError, socket.timeout):
        return reply('timeout', '连接或读取超时，请检查模型服务与网络。')
    except URLError as exc:
        return reply('timeout' if isinstance(exc.reason, (TimeoutError, socket.timeout)) else 'unreachable',
                     '无法连接模型服务，请检查地址、端口、证书与网络。')
    except (ValueError, UnicodeError):
        return reply('invalid_response', '服务响应无法解析为模型列表。')
    except OSError:
        return reply('unreachable', '无法连接模型服务，请检查服务和网络。')


def video_report():
    from ..video.media import capabilities
    try:
        report = capabilities()
        # Reuse the production check but return only the curated, actionable fields.
        return dict(checked_at=utc_now_iso(), ok=True, ready=report['ready'], checks=report['checks'])
    except Exception:
        return dict(checked_at=utc_now_iso(), ok=False, ready=False, checks=[],
                    message='视频环境检查未完成，请检查编码程序、字体及安装依赖后重试。')


def register_readiness_routes(app):
    from fastapi.responses import JSONResponse
    @app.get('/api/setup/check')
    def check():
        from sqlalchemy.exc import SQLAlchemyError
        try:
            report = configuration_report(app)
            return JSONResponse(report, headers={'Cache-Control': 'no-store'})
        except SQLAlchemyError:
            return JSONResponse({'message': '无法读取数据库，请检查连接和访问权限。'}, status_code=503,
                                headers={'Cache-Control': 'no-store'})

    @app.post('/api/setup/model-probe')
    def model_probe():
        return JSONResponse(probe_model(), headers={'Cache-Control': 'no-store'})

    @app.post('/api/setup/video-check')
    def video_check():
        return JSONResponse(video_report(), headers={'Cache-Control': 'no-store'})
