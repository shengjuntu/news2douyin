"""Profile and schedule lifecycle; all mutations share the collection write lock."""
from sqlmodel import select

from ..scheduler.cron import next_occurrences, parse_cron
from ..search.dates import timezone_name
from ..storage.articles import lock_news
from ..storage.models import CollectProfile, ProfileState, CollectJob, TaskRecord, RunRecord, utc_now_iso
from ..tasks.control import TaskConflict


def require_profile(session, name, *, enabled=False):
    row = session.exec(select(CollectProfile).where(CollectProfile.name == name)).first()
    if row is None:
        raise KeyError('采集策略不存在')
    if enabled and not profile_enabled(session, name):
        raise TaskConflict('策略已停用，请先启用策略')
    return row


def profile_enabled(session, name):
    state = session.get(ProfileState, name)
    return state is None or state.enabled


def profile_links(session, name):
    jobs = list(session.exec(select(CollectJob).where(CollectJob.profile_name == name, CollectJob.schedule_type != 'archived')
                            .order_by(CollectJob.id)))
    tasks = list(session.exec(select(TaskRecord.task_id).where(TaskRecord.profile_name == name,
                              TaskRecord.status.in_(['queued', 'running', 'cancel_requested']))))
    direct_runs = list(session.exec(select(RunRecord.id).where(RunRecord.profile_name == name, RunRecord.status == 'running',
                       ~RunRecord.id.in_(select(TaskRecord.run_id).where(TaskRecord.run_id != None)))))
    return jobs, tasks, direct_runs


def set_profile_enabled(session, name, enabled):
    lock_news(session)
    require_profile(session, name)
    state = session.get(ProfileState, name) or ProfileState(name=name)
    state.enabled, state.updated_at = enabled, utc_now_iso()
    paused = []
    if not enabled:
        for job in profile_links(session, name)[0]:
            if job.enabled:
                job.enabled, job.updated_at = False, utc_now_iso()
                paused.append(job.id)
                session.add(job)
    session.add(state)
    session.commit()
    return {'name': name, 'enabled': enabled, 'paused_job_ids': paused}


def delete_profile(session, name):
    lock_news(session)
    row = require_profile(session, name)
    jobs, tasks, direct_runs = profile_links(session, name)
    if jobs:
        raise TaskConflict('请先删除或改绑关联计划：' + '、'.join(j.name for j in jobs))
    if tasks or direct_runs:
        raise TaskConflict('该策略仍有未结束任务，请等待完成或取消任务后再删除')
    state = session.get(ProfileState, name)
    if state:
        session.delete(state)
    session.delete(row)
    session.commit()
    return {'deleted': True, 'name': name, 'history_preserved': True}


def job_dict(row, *, preview=True):
    result = {name: getattr(row, name) for name in ('id', 'name', 'enabled', 'timezone', 'cron_expr', 'profile_name',
              'auto_editorial', 'auto_video', 'auto_tts', 'last_run_at', 'last_status')}
    result.update(next_runs=[], validation_error='', archived=row.schedule_type == 'archived')
    if preview:
        try:
            result['next_runs'] = next_occurrences(row.cron_expr, row.timezone)
            if not result['next_runs']:
                result['validation_error'] = '未来五年没有匹配的日期，请检查计划'
        except ValueError as exc:
            result['validation_error'] = str(exc)
    return result


def save_job(session, data, job_id=None):
    lock_news(session)
    data = dict(data)
    name = str(data.get('name', '')).strip()
    if not name or len(name) > 100:
        raise ValueError('计划名称不能为空且最多 100 字符')
    data['name'] = name
    data['cron_expr'] = ' '.join(data['cron_expr'].split())
    data['timezone'] = timezone_name(data.get('timezone', 'UTC'))
    parse_cron(data['cron_expr'])
    if not next_occurrences(data['cron_expr'], data['timezone'], count=1):
        raise ValueError('未来五年没有匹配的执行日期，请检查 Cron')
    require_profile(session, data['profile_name'], enabled=data.get('enabled', True))
    existing = session.exec(select(CollectJob).where(CollectJob.name == name)).first()
    if job_id is not None:
        row = session.get(CollectJob, job_id)
        if row is None or row.schedule_type == 'archived':
            raise KeyError('计划不存在')
        if existing and existing.id != row.id:
            raise TaskConflict('计划名称已使用，请换一个名称')
    else:
        row = existing or CollectJob(name=name, profile_name=data['profile_name'])
    if row.schedule_type == 'archived':
        raise TaskConflict('该计划名称已归档，请使用新名称')
    for field in ('name', 'enabled', 'timezone', 'cron_expr', 'profile_name', 'auto_editorial', 'auto_video', 'auto_tts'):
        if field in data:
            setattr(row, field, data[field])
    row.updated_at = utc_now_iso()
    session.add(row)
    session.commit()
    session.refresh(row)
    return job_dict(row)


def set_job_enabled(session, job_id, enabled):
    lock_news(session)
    row = session.get(CollectJob, job_id)
    if row is None or row.schedule_type == 'archived':
        raise KeyError('计划不存在')
    if enabled:
        require_profile(session, row.profile_name, enabled=True)
        if not next_occurrences(row.cron_expr, row.timezone, count=1):
            raise ValueError('计划没有可执行日期，请先修改')
    row.enabled, row.updated_at = enabled, utc_now_iso()
    session.add(row)
    session.commit()
    return job_dict(row)


def delete_job(session, job_id):
    lock_news(session)
    row = session.get(CollectJob, job_id)
    if row is None:
        raise KeyError('计划不存在')
    # Preserve identity: a deleted SQLite row ID must not be reused by a new
    # plan while an old task still refers to it.
    row.enabled, row.schedule_type, row.updated_at = False, 'archived', utc_now_iso()
    session.add(row)
    session.commit()
    return {'deleted': True, 'id': job_id, 'history_preserved': True}
