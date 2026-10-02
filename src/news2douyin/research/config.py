"""Runtime-only configuration. Secrets never appear in public settings responses."""
import json
import os
import secrets
import threading
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, ConfigDict


class SettingsInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    rundesk_url: str = 'http://127.0.0.1:3210'
    rundesk_token: str = ''
    callback_url: str = 'http://127.0.0.1:18080'
    search_provider: str = 'worldnewsapi'
    search_api_key: str = ''


def base_url(value):
    try:
        p = urlsplit(value)
        if p.scheme not in {'http', 'https'} or not p.hostname or p.username or p.password or p.query or p.fragment:
            raise ValueError()
        _ = p.port
    except ValueError:
        raise ValueError('服务地址必须为无凭据、无查询参数的 HTTP(S) 地址') from None
    return value.rstrip('/')


class SettingsStore:
    def __init__(self, root):
        self.path = Path(root) / 'research' / 'settings.json'
        self.lock = threading.RLock()

    def get(self):
        with self.lock:
            if self.path.exists():
                return json.loads(self.path.read_text(encoding='utf-8'))
            data = dict(rundesk_url='http://127.0.0.1:3210', rundesk_token='',
                        callback_url='http://127.0.0.1:18080', search_provider='worldnewsapi', search_api_key='',
                        installation_id=secrets.token_hex(12), mcp_token=secrets.token_urlsafe(32),
                        instance_id='', workspace_id='', skill_path='', codex_home='', setup_message='尚未配置')
            self.write(data)
            return data

    def write(self, data):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name('settings-' + secrets.token_hex(8) + '.tmp')
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)

    def public(self):
        data = self.get()
        for key in ('rundesk_token', 'search_api_key', 'mcp_token'):
            data[key + '_configured'] = bool(data.pop(key, ''))
        env = 'TAVILY_API_KEY' if data['search_provider'] == 'tavily' else 'API_KEY'
        data['search_ready'] = data['search_api_key_configured'] or bool(os.getenv(env))
        return data

    def update(self, payload):
        with self.lock:
            data = self.get()
            incoming = SettingsInput.model_validate(payload).model_dump()
            incoming['rundesk_url'] = base_url(incoming['rundesk_url']).removesuffix('/api')
            incoming['callback_url'] = base_url(incoming['callback_url'])
            if incoming['search_provider'] not in {'worldnewsapi', 'tavily'}:
                raise ValueError('搜索服务应为 worldnewsapi 或 tavily')
            changed_host = incoming['rundesk_url'] != data['rundesk_url']
            changed_provider = incoming['search_provider'] != data['search_provider']
            for key in ('rundesk_token', 'search_api_key'):
                if not incoming[key] and not (changed_host if key == 'rundesk_token' else changed_provider):
                    incoming[key] = data[key]
            if changed_host:
                data.update(instance_id='', workspace_id='', skill_path='', codex_home='')
            data.update(incoming)
            data['setup_message'] = '设置已保存，请配置或检查研究助手'
            self.write(data)
            return self.public()


class StrategyInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=100)
    questions: list[str] = Field(min_length=1, max_length=20)
    preferred_domains: list[str] = Field(default_factory=list, max_length=30)
    language: str = Field(default='zh', max_length=12)
    max_searches: int = Field(default=10, ge=1, le=40)
    max_reads: int = Field(default=20, ge=1, le=60)
    max_minutes: int = Field(default=20, ge=1, le=120)
    enabled: bool = True
    revision: int = 0


DEFAULT_STRATEGIES = [
    ('background', '通用背景研究', ['核心事实有哪些原始依据？', '此前有哪些相关事件和转折？', '各方解释是否存在矛盾？', '哪些影响已经发生，哪些仍待验证？']),
    ('policy', '政策变化研究', ['找到政策原文及发布日期、生效日期', '与旧版本相比具体改变了什么？', '此前有哪些实施记录？', '各方解读分别依据什么，哪些问题尚未明确？']),
    ('company', '公司事件研究', ['查找公司原始公告与历史披露', '事件此前的发展过程是什么？', '相关方有哪些回应或相反证据？', '区分已披露事实与市场推测']),
]
