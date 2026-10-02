"""RunDesk v1 adapter. Operation keys and payloads are persisted by callers."""
from importlib.resources import files
from urllib.parse import quote
import re
import secrets

import requests


class RemoteError(ValueError):
    def __init__(self, message, *, uncertain=False, status=0, code='', request_id='', retryable=False):
        super().__init__(message)
        self.uncertain, self.status, self.code = uncertain, status, code
        self.request_id, self.retryable = request_id, retryable


def remote_error(status, body, method='POST'):
    body = body if isinstance(body, dict) else {}
    code = str(body.get('code', ''))[:100]
    request_id = str(body.get('requestId', ''))[:128]
    hints = {'unauthorized':'认证失败，请检查 RunDesk Token', 'forbidden':'访问被拒绝，请检查地址和权限',
             'idempotency_conflict':'同一请求编号的内容发生变化，请核对已保存的提交',
             'request_in_progress':'原请求仍在处理，将继续查询同一回执',
             'request_unconfirmed':'原请求结果未确认；保留原编号，请在 RunDesk 核对，不会创建替代任务',
             'run_conflict':'会话已经进入另一项运行，未操作该运行',
             'revision_conflict':'配置版本已变化，请重新读取后再保存'}
    hint = hints.get(code, {401:'认证失败，请检查 RunDesk Token',404:'资源或 v1 接口不存在，请确认 RunDesk 0.6.0 或更新版本',409:'运行或配置冲突，请同步后重试'}.get(status,'请检查 RunDesk 状态'))
    suffix = (' [' + code + ']') if re.fullmatch(r'[a-zA-Z0-9_]{1,100}',code) else ''
    if re.fullmatch(r'[a-zA-Z0-9_.:-]{8,128}',request_id): suffix += ' 请求号：' + request_id
    return RemoteError(f'RunDesk HTTP {status}：{hint}{suffix}', uncertain=(method!='GET' and status>=500) or code in {'request_in_progress','request_unconfirmed'},
                       status=status, code=code, request_id=request_id, retryable=body.get('retryable') is True)


class RunDeskClient:
    def __init__(self, config):
        self.base = config['rundesk_url'].rstrip('/').removesuffix('/api/v1').removesuffix('/api') + '/api/v1'
        self.token = config.get('rundesk_token', '')

    def request(self, method, path, payload=None, params=None, *, key=None):
        headers = {'Accept':'application/json'}
        if self.token: headers['Authorization'] = 'Bearer ' + self.token
        if key: headers['Idempotency-Key'] = key
        try:
            response = requests.request(method, self.base + path, json=payload, params=params,
                                        headers=headers, timeout=(4,20), allow_redirects=False)
        except requests.RequestException:
            raise RemoteError('RunDesk 连接失败或超时；将用原请求编号查询接收结果',uncertain=method!='GET') from None
        if len(response.content) > 4*1024*1024:
            raise RemoteError('RunDesk 响应过大',uncertain=method!='GET')
        try: value=response.json()
        except ValueError:
            raise RemoteError('RunDesk 返回格式无效',uncertain=method!='GET',status=response.status_code) from None
        if response.status_code not in {200,201,202}:
            raise remote_error(response.status_code,value,method)
        return value


def require_v1(client):
    meta=client.request('GET','/meta')
    if 'v1' not in meta.get('apiVersions',[]) or not {'api-v1','idempotency','configuration-summary'}.issubset(meta.get('capabilities',[])):
        raise ValueError('需要支持 API v1、提交回执和配置总览的 RunDesk 0.6.0 或更新版本')
    return meta


def submit_once(client, key, path, payload, *, allow_send=True):
    """Reconcile first; only a missing receipt permits the identical submission.

    Never replace an unconfirmed key. A completed receipt may itself be an HTTP
    failure and does not mean the model run has completed.
    """
    try:
        receipt=client.request('GET','/requests/'+quote(key,safe=''))
    except RemoteError as exc:
        if exc.status!=404 or exc.code!='request_not_found':
            # Failure to read a receipt says nothing about the original write.
            exc.uncertain=True
            raise
        if not allow_send:
            raise RemoteError('尚未找到提交回执，继续等待确认；不会为停止操作重新启动任务',uncertain=True,code='request_unconfirmed')
        return client.request('POST',path,payload,key=key)
    if receipt.get('method')!='POST' or receipt.get('path') != path:
        raise RemoteError('回执与保存的请求不一致，请核对 RunDesk 数据目录',uncertain=True,code='receipt_mismatch')
    if receipt.get('state')!='completed':
        raise remote_error(409,{'code':'request_in_progress' if receipt.get('state')=='processing' else 'request_unconfirmed','retryable':receipt.get('state')=='processing'})
    status=receipt.get('httpStatus',0)
    if not isinstance(status,int) or not 200<=status<300:
        raise remote_error(status,receipt.get('response'))
    return receipt.get('response')


def setup_submission(settings,config,client,kind,path,payload):
    operations=config.setdefault('setup_requests',{})
    if kind not in operations:
        operations[kind]={'key':'n2d-'+secrets.token_hex(16),'path':path,'payload':payload}
        settings.write(config)
    op=operations[kind]
    if op['path']!=path or op['payload']!=payload:
        raise ValueError('研究助手的创建请求已保存，请先核对配置，不能替换未确认请求')
    return submit_once(client,op['key'],op['path'],op['payload'])


def bootstrap(settings, client_factory=RunDeskClient):
    # Serializes setup and each checkpoint, including partial remote setup.
    with settings.lock:
        config = settings.get()
        client = client_factory(config)
        meta = require_v1(client)
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
            instance = matches[0] if matches else setup_submission(settings, config, client, 'instance', '/instances',
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
            workspace = matches[0] if matches else setup_submission(settings, config, client, 'workspace', '/workspaces', {'name': workspace_name, 'path': ''})
            config['workspace_id'] = workspace['id']
            settings.write(config)
        running = client.request('GET','/sessions',params={'instanceId':config['instance_id']})
        if any(v.get('status') in {'starting','running','waiting','stopping'} for v in running):
            raise ValueError('研究实例还有活动任务，请结束后再更新工具配置')
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
    meta = require_v1(client)
    result = {'version': meta.get('version'), 'demo': meta.get('demo'), 'configured': bool(config['instance_id'] and config['skill_path'])}
    if result['configured']:
        path = '/workspaces/' + quote(config['workspace_id'], safe='')
        params = {'instanceId': config['instance_id']}
        account = client.request('GET', path + '/account', params=params)
        result['account_ready'] = bool(account.get('account')) or account.get('requiresOpenaiAuth') is False
        overview = client.request('GET','/instances/'+quote(config['instance_id'],safe='')+'/configuration',params={'workspaceId':config['workspace_id'],'probe':'1'})
        result['default_model'] = overview.get('instance',{}).get('defaultModel','')
        result['skills'] = [{'name':v.get('name'), 'scope':v.get('sourceScope'), 'enabled':v.get('enabled')} for v in overview.get('skills',[])]
        result['configuration_errors'] = overview.get('errors',{})
        state = overview.get('mcp',{}).get('status') or {}
        entries = state.get('data', []) if isinstance(state, dict) else state if isinstance(state, list) else []
        own = next((v for v in entries if v.get('name') == 'news2douyin-research'), {})
        tools = own.get('tools') or {}
        result['tools'] = sorted(tools) if isinstance(tools, dict) else [x.get('name', '') for x in tools]
        result['mcp_ready'] = 'research_get_case' in result['tools']
        result['message'] = '配置和工具检查完成；尚未验证真实研究质量'
    return result
