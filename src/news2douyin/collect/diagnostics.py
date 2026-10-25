"""Read-only diagnostics from the frozen task and actual filter decisions."""
from ..storage.models import RunRecord, TaskRecord, TaskCheckpoint
from ..storage.utils import loads
from ..tasks.service import task_dict


REASONS = {'source_not_allowed': '不在来源白名单', 'source_blocked': '命中来源黑名单',
           'excluded_keyword': '命中排除关键词', 'not_relevant_rule': '未命中关键词或分类规则',
           'not_relevant_ai': 'AI 判为不相关', 'kept_rule': '规则保留', 'kept_ai': 'AI 保留'}
FALLBACKS = {'configured_rules': '按策略配置使用规则筛选', 'model_unavailable': '模型不可用，使用规则筛选',
             'request_failed': '模型请求失败，剩余新闻使用规则筛选',
             'invalid_response': '模型返回格式无效，剩余新闻使用规则筛选',
             'partial_response': '模型遗漏部分新闻，遗漏项使用规则筛选'}


def summary(stats, status='succeeded', error=''):
    if status == 'failed':
        text = (error or '').lower()
        if 'missing env' in text:
            return '新闻源凭据未配置，请在服务端 .env 中设置对应 API Key，再重试。'
        if '429' in text or 'quota' in text or '402' in text:
            return '上游可能触发额度或频率限制，请检查账户额度和采集频率。'
        if '401' in text or '403' in text:
            return '上游拒绝访问，请检查 API Key、账户权限及请求条件。'
        if 'timeout' in text or 'timed out' in text:
            return '请求超时，请检查服务连接与超时设置后重试。'
        return '任务执行失败；已取得的筛选结果会保留，请结合任务错误信息检查后重试。'
    if status in {'queued', 'running', 'cancel_requested'}:
        return '任务尚未结束，下面仅展示已经保存的诊断结果。'
    if status == 'cancelled':
        return '任务已取消；下面仅展示取消前保存的诊断结果。'
    diagnostic = stats.get('diagnostics') or {}
    if not diagnostic:
        return '这条历史运行没有逐项筛选记录；可重新试跑获得诊断。'
    if stats.get('fetched', 0) == 0:
        return '新闻源返回 0 条。请检查采集日期、国家、语言和新闻源供应情况；此时还没有执行内容筛选。'
    if diagnostic.get('after_hard_filter') == 0:
        return '全部新闻被来源限制或排除词过滤。请查看原因计数，调整名单或排除词后试跑。'
    if stats.get('after_filter', 0) == 0:
        return '新闻通过了来源检查，但均未通过相关性筛选。可放宽关注关键词或分类，再比较结果。'
    if stats.get('dry_run'):
        return f"试跑保留 {stats.get('after_filter', 0)} 条；没有写入新闻库，也没有执行文章去重。正式采集后再显示新增、更新和重复抓取数量。"
    if not stats.get('stored_articles') and not stats.get('updated_articles') and stats.get('skipped_existing'):
        return '筛选后的新闻已存在且内容未变，本次没有新增；仍可从“选择本次新闻”继续选题。'
    return f"本次保留 {stats.get('after_filter', 0)} 条，新增 {stats.get('stored_articles', 0)} 条、更新 {stats.get('updated_articles', 0)} 条。"


def task_diagnostics(session, task_id):
    task = session.get(TaskRecord, task_id)
    if task is None:
        raise KeyError('任务不存在')
    run = session.get(RunRecord, task.run_id) if task.run_id else None
    stats = loads(run.stats_json, {}) if run else {}
    if not stats.get('diagnostics'):
        checkpoint = session.get(TaskCheckpoint, task_id + ':filtered')
        if checkpoint:
            saved = loads(checkpoint.payload_json, {})
            diagnostic = saved.get('diagnostics') or {}
            stats = dict(stats, diagnostics=diagnostic, fetched=diagnostic.get('fetched', 0),
                         after_filter=len(saved.get('items', [])), filter_mode=saved.get('mode', ''))
    stats['dry_run'] = task.trigger_type == 'profile_test'
    profile = loads(task.profile_json, {})
    fields = ('name', 'provider', 'country', 'language', 'categories', 'keywords_include', 'keywords_exclude',
              'source_whitelist', 'source_blacklist', 'max_items', 'date_str', 'timezone', 'filter_mode', 'collection_mode', 'search_query')
    return {'task': task_dict(task), 'stats': stats, 'profile': {k: profile[k] for k in fields if k in profile},
            'summary': summary(stats, task.status, task.error_text), 'reasons': REASONS, 'fallbacks': FALLBACKS}
