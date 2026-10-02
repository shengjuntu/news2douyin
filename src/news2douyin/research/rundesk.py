"""RunDesk v0.5.4 HTTP adapter. Mutating requests are never blindly retried."""
from importlib.resources import files
from urllib.parse import quote

import requests


class RemoteError(ValueError):
    def __init__(self, message, *, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


class RunDeskClient:
    def __init__(self, config):
        self.base = config['rundesk_url'].rstrip('/') + '/api'
        self.token = config.get('rundesk_token', '')

    def request(self, method, path, payload=None, params=None):
        try:
            headers = {'Accept': 'application/json'}
            if self.token:
                headers['Authorization'] = 'Bearer ' + self.token
            response = requests.request(method, self.base + path, json=payload, params=params,
                                        headers=headers, timeout=(4, 20), allow_redirects=False)
            if response.status_code not in {200, 201, 202}:
                hint = {401: '认证失败，请检查 RunDesk Token', 403: '访问被拒绝，请检查地址和权限',
                        404: '接口或资源不存在，请确认 RunDesk 为 v0.5.4 或兼容版本',
                        409: '运行或配置发生冲突，请同步状态后重试'}.get(response.status_code, '请检查 RunDesk 状态')
                raise RemoteError(f'RunDesk HTTP {response.status_code}：{hint}')
            if len(response.content) > 4 * 1024 * 1024:
                raise RemoteError('RunDesk 响应过大')
            return response.json()
        except requests.RequestException:
            raise RemoteError('RunDesk 连接失败或超时；写操作的结果需要同步确认', uncertain=method != 'GET') from None
        except requests.exceptions.JSONDecodeError:
            raise RemoteError('RunDesk 返回格式无效', uncertain=method != 'GET') from None


def bootstrap(settings, client_factory=RunDeskClient):
    # Serializes setup and each checkpoint, including partial remote setup.
    with settings.lock:
        config = settings.get()
        client = client_factory(config)
        meta = client.request('GET', '/meta')
        if meta.get('demo'):
            raise ValueError('RunDesk 当前为演示模式，无法执行真实研究')
        if not {'instances', 'skills', 'mcp', 'sessions'}.issubset(set(meta.get('capabilities', []))):
            raise ValueError('RunDesk 缺少实例、Skill、MCP 或会话接口')
        marker = 'news2douyin-research:' + config['installation_id']
        instances = client.request('GET', '/instances')
        instance = next((r for r in instances if r['id'] == config['instance_id']), None)
        if config['instance_id'] and not instance:
            raise ValueError('已绑定的研究实例不存在；请检查 RunDesk 数据目录')
        if not instance:
            matches = [r for r in instances if r.get('description') == marker]
            if len(matches) > 1:
                raise ValueError('发现多个相同应用实例，请在 RunDesk 中核对后再配置')
            instance = matches[0] if matches else client.request('POST', '/instances',
                            {'name': '新闻研究助手', 'description': marker, 'defaultModel': ''})
            config['instance_id'] = instance['id']
            config['codex_home'] = instance.get('codexHome', '')
            settings.write(config)
        workspace_name = 'news2douyin-research-' + config['installation_id']
        config['codex_home'] = instance.get('codexHome', config.get('codex_home', ''))
        workspaces = client.request('GET', '/workspaces')
        workspace = next((r for r in workspaces if r['id'] == config['workspace_id']), None)
        if config['workspace_id'] and not workspace:
            raise ValueError('已绑定的研究工作区不存在')
        if not workspace:
            matches = [r for r in workspaces if r.get('name') == workspace_name]
            if len(matches) > 1:
                raise ValueError('发现多个研究工作区，请核对 RunDesk 配置')
            workspace = matches[0] if matches else client.request('POST', '/workspaces', {'name': workspace_name, 'path': ''})
            config['workspace_id'] = workspace['id']
            settings.write(config)
        path = '/workspaces/' + quote(config['workspace_id'], safe='')
        params = {'instanceId': config['instance_id']}
        content = files('news2douyin.research').joinpath('news-research.md').read_text(encoding='utf-8')
        client.request('PUT', path + '/skills/news-research', {'content': content}, {**params, 'scope': 'instance'})
        current = client.request('GET', path + '/mcp', params=params)
        result = client.request('PUT', path + '/mcp/news2douyin-research', {
            'version': current['version'],
            'config': {'url': config['callback_url'] + '/mcp/research',
                       'http_headers': {'Authorization': 'Bearer ' + config['mcp_token']},
                       'enabled': True, 'tool_timeout_sec': 90}}, params)
        if result.get('reloadError'):
            config['setup_message'] = '配置已保存，但 MCP 重载失败；请检查回连地址并重新配置'
            settings.write(config)
            raise ValueError(config['setup_message'])
        skills = client.request('GET', path + '/skills', params=params)
        rows = [skill for group in skills.get('data', []) for skill in group.get('skills', [])]
        expected = config['codex_home'].replace('\\', '/').rstrip('/') + '/skills/news-research/SKILL.md'
        skill = next((r for r in rows if r.get('name') == 'news-research'
                      and r.get('path', '').replace('\\', '/') == expected), None)
        if not skill:
            raise ValueError('Skill 已写入，但 Codex 尚未发现；请检查该实例的运行环境')
        if not skill.get('enabled'):
            client.request('POST', path + '/skills/toggle', {'path': skill['path'], 'enabled': True}, params)
        config['skill_path'] = skill['path']
        config['setup_message'] = '实例、研究 Skill 和 MCP 已配置；请检查认证和工具连通性后运行'
        settings.write(config)
        return settings.public()


def connection_check(settings, client_factory=RunDeskClient):
    config = settings.get()
    client = client_factory(config)
    meta = client.request('GET', '/meta')
    result = {'version': meta.get('version'), 'demo': meta.get('demo'), 'configured': bool(config['instance_id'] and config['skill_path'])}
    if result['configured']:
        path = '/workspaces/' + quote(config['workspace_id'], safe='')
        params = {'instanceId': config['instance_id']}
        account = client.request('GET', path + '/account', params=params)
        result['account_ready'] = bool(account.get('account')) or account.get('requiresOpenaiAuth') is False
        mcp = client.request('GET', path + '/mcp', params=params)
        state = mcp.get('status') or {}
        entries = state.get('data', []) if isinstance(state, dict) else state if isinstance(state, list) else []
        own = next((v for v in entries if v.get('name') == 'news2douyin-research'), {})
        tools = own.get('tools') or {}
        result['tools'] = sorted(tools) if isinstance(tools, dict) else [x.get('name', '') for x in tools]
        result['mcp_ready'] = 'research_get_case' in result['tools']
        result['message'] = '配置和工具检查完成；尚未验证真实研究质量'
    return result
