"""Runtime-only configuration. Secrets never appear in public settings responses."""
import json
import os
import secrets
import threading
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, ConfigDict


def require_search_route(provider, backend):
    if provider != 'rundesk' or backend != 'exa':
        raise ValueError('研究检索方式已升级：请停止旧轮次，连接配置后在原课题开始新一轮。'
                         '新研究由 Codex 调用 Exa；news2douyin 不再直连外部搜索。'
                         '原有证据和报告保留，此提示不表示搜索预算耗尽。')


class SettingsInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    rundesk_url: str = 'http://127.0.0.1:3210'
    rundesk_token: str = ''
    callback_url: str = ''
    search_provider: str = 'rundesk'
    search_api_key: str = ''
    runtime_mode: str = 'docker'
    docker_image: str = ''
    docker_permissions: str = 'full-access'
    mcp_callback_url: str = ''
    search_backend: str = 'exa'


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
        self.setup_lock = threading.Lock()

    def get(self):
        with self.lock:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding='utf-8'))
                # Existing installations retain their execution mode until the
                # administrator explicitly chooses Docker in research settings.
                data.setdefault('runtime_mode', 'local')
                data.setdefault('docker_image', '')
                data.setdefault('docker_permissions', 'full-access')
                data.setdefault('mcp_callback_url', '')
                data.setdefault('search_backend', 'exa')
                if data.get('search_provider') in {'worldnewsapi', 'tavily'} or data['search_backend'] == 'wigolo':
                    # Migrate defaults only. Existing run snapshots and evidence
                    # retain provenance; legacy runs must be explicitly restarted.
                    data['previous_search_route'] = {'provider': data.get('search_provider'), 'backend': data['search_backend']}
                    data['search_provider'] = 'rundesk'
                    data['search_backend'] = 'exa'
                    data['search_route_notice'] = '研究搜索已统一为 Codex / Exa。请连接并配置环境，停止旧轮次后开始新一轮；采集配置、证据和报告保留。'
                    data['setup_message'] = data['search_route_notice']
                    self.write(data)
                return data
            data = dict(rundesk_url='http://127.0.0.1:3210', rundesk_token='',
                        callback_url='http://127.0.0.1:18080', search_provider='rundesk', search_api_key='',
                        runtime_mode='docker', docker_image='', docker_permissions='full-access', mcp_callback_url='', search_backend='exa',
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
        data.pop('setup_requests', None)
        data.pop('docker_initialized_environments', None)
        data['search_ready'] = None  # Only RunDesk tool discovery can verify readiness.
        data['search_mode'] = 'codex-mcp'
        return data

    def update(self, payload):
        with self.setup_lock, self.lock:
            data = self.get()
            incoming = SettingsInput.model_validate(payload).model_dump()
            for key in ('runtime_mode', 'docker_image', 'docker_permissions', 'mcp_callback_url', 'search_backend', 'search_provider'):
                if key not in payload:
                    incoming[key] = data[key]
            incoming['rundesk_url'] = base_url(incoming['rundesk_url']).removesuffix('/api/v1').removesuffix('/api')
            incoming['callback_url'] = base_url(incoming['callback_url'] or data['callback_url'])
            require_search_route(incoming['search_provider'], incoming['search_backend'])
            if incoming['search_api_key']:
                raise ValueError('研究搜索账号请在 RunDesk 的 Exa 配置中管理；新闻采集账号仍在采集配置中管理')
            if incoming['runtime_mode'] not in {'docker', 'local'} or incoming['search_backend'] != 'exa':
                raise ValueError('执行模式或 MCP 搜索后端无效')
            if incoming['docker_permissions'] not in {'full-access', 'preserve'}:
                raise ValueError('Docker 权限应为 full-access 或 preserve')
            if incoming['mcp_callback_url']:
                incoming['mcp_callback_url'] = base_url(incoming['mcp_callback_url'])
            import re
            if incoming['docker_image'] and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./:@-]{0,254}', incoming['docker_image']):
                raise ValueError('Docker 镜像引用无效')
            changed_host = incoming['rundesk_url'] != data['rundesk_url']
            changed_provider = incoming['search_provider'] != data['search_provider']
            for key in ('rundesk_token', 'search_api_key'):
                if not incoming[key] and not (changed_host if key == 'rundesk_token' else changed_provider):
                    incoming[key] = data[key]
            if changed_host:
                data.update(instance_id='', workspace_id='', skill_path='', codex_home='', setup_requests={}, managed_by_rundesk=False, environment_id='', docker_initialized_environments=[])
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
