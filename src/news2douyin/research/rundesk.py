"""RunDesk v1 adapter. Operation keys and payloads are persisted by callers."""
from importlib.resources import files
from copy import deepcopy
from urllib.parse import quote, urlsplit, urlunsplit
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
             'revision_conflict':'配置版本已变化，请重新读取后再保存',
             'application_binding_changed':'应用绑定已变化，请重新连接 RunDesk',
             'docker_configuration_required':'请在研究设置填写已构建的 Docker 镜像并连接，或在 RunDesk 选择镜像',
             'environment_update_required':'RunDesk 中有待部署的镜像或资源变更，请显式更新环境',
             'docker_unavailable':'RunDesk 主机的 Docker Engine 不可用',
             'docker_runtime_invalid':'镜像内 Codex 或进程监管不可用，请检查镜像构建与主机环境',
             'docker_seed_failed':'容器配置初始化失败，请检查镜像及挂载目录权限'}
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
                                        headers=headers, timeout=(10,180 if path.endswith('/runtime/prepare') else 60), allow_redirects=False)
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
    # Network initialization may call this service's MCP endpoint, which reads
    # settings for authentication. Never hold the settings read lock across I/O.
    with settings.setup_lock:
        config = settings.get()
        client = client_factory(config)
        meta = require_v1(client)
        if meta.get('demo'):
            raise ValueError('RunDesk 当前为演示模式，无法执行真实研究')
        if 'application-connect' not in meta.get('capabilities', []):
            raise ValueError('请先升级到支持应用自动注册的 RunDesk 0.15.0')
        docker = config.get('runtime_mode') == 'docker'
        if docker and 'application-runtime' not in meta.get('capabilities', []):
            raise ValueError('一键 Docker 研究需要 RunDesk 0.47.0 或更新版本')
        if docker and config.get('docker_permissions') == 'full-access' and 'mcp-server-approval-policy' not in meta.get('capabilities', []):
            raise ValueError('Docker 完整访问与 MCP 持续授权需要 RunDesk 0.47.1 或更新版本；请先升级 RunDesk')
        content = files('news2douyin.research').joinpath('news-research.md').read_text(encoding='utf-8')
        result = client.request('POST', '/applications/news2douyin/connect', {
            'name': '新闻研究', 'description': '研究选定新闻的历史背景、来源、争议和影响；通过业务工具保存证据与报告。研究任务须包含 news2douyin 的 run_id。',
            'entryUrl': config['callback_url'], 'installationId': config['installation_id'],
            'instanceId': config.get('instance_id', ''), 'workspaceId': config.get('workspace_id', ''),
            'defaults': {} if docker else {'skills': {'news-research': {'SKILL.md': content}},
                         'mcp': {'news2douyin-research': {'url': config['callback_url'] + '/mcp/research',
                                 'http_headers': {'Authorization': 'Bearer ' + config['mcp_token']},
                                 'enabled': True, 'tool_timeout_sec': 90}}}})
        app = result['application']
        config.update(instance_id=app['instanceId'], workspace_id=app['workspaceId'],
                      managed_by_rundesk=True, connection_status=result['status'],
                      setup_message=result['message'], management_path=result['managementPath'])
        settings.write(config)
        if docker:
            setup_docker(settings, client, content)
        else:
            configure_exa(client, config)
        return settings.public()


def callback_for_docker(config):
    if config.get('mcp_callback_url'):
        return config['mcp_callback_url']
    url = urlsplit(config['callback_url'])
    loopback = {'localhost', '127.0.0.1', '::1'}
    if url.hostname in loopback:
        if urlsplit(config['rundesk_url']).hostname not in loopback:
            raise ValueError('RunDesk 在另一台机器，请填写容器能够访问的 news2douyin MCP 回调地址')
        return urlunsplit((url.scheme, 'host.docker.internal' + ((':' + str(url.port)) if url.port else ''), url.path, '', ''))
    return config['callback_url']


def prepare_runtime(client, config, *, probe=True):
    return client.request('POST', '/applications/news2douyin/runtime/prepare', {
        'instanceId': config['instance_id'], 'workspaceId': config['workspace_id'], 'probe': probe})


def setup_docker(settings, client, skill):
    """Explicit administrator setup; ordinary starts never rewrite configuration."""
    config = settings.get()
    callback = callback_for_docker(config)
    iid, wid = quote(config['instance_id'], safe=''), quote(config['workspace_id'], safe='')
    image = config.get('docker_image')
    for attempt in range(2):
        instance = client.request('GET', '/instances/' + iid + '/configuration', params={'workspaceId': config['workspace_id']})['instance']
        if ((instance.get('agentRuntime') or {}).get('kind') or 'codex') != 'codex':
            raise ValueError('新闻 Docker 完整访问配置只支持 Codex 实例')
        execution = instance.get('execution') or {}
        if execution.get('mode', 'local') == 'docker':
            if image and execution.get('image') != image:
                raise ValueError('填写的镜像与已绑定镜像不同；请在 RunDesk 选择并显式更新环境，避免替换现有任务')
            break
        if not image:
            raise ValueError('应用已登记。请先到 RunDesk 初始化向导准备 news2douyin 镜像，再填写镜像地址；也可在 RunDesk 为此应用选择已有镜像')
        try:
            client.request('PUT', '/instances/' + iid + '/execution', {'revision': instance['revision'],
                'spec': {'mode': 'docker', 'image': image, 'cpus': 2, 'memoryMB': 4096, 'pidsLimit': 512,
                         'idleMinutes': 15, 'network': 'bridge'}})
            break
        except RemoteError as exc:
            # Only a revision conflict permits one fresh read and retry. A
            # changed image/backend is checked again; other failures propagate.
            if attempt or exc.status != 409 or exc.code != 'revision_conflict':
                raise
    runtime = prepare_runtime(client, config, probe=False)
    environment_id = runtime.get('environment', {}).get('id', '')
    if not environment_id or runtime.get('executionMode') != 'docker' or runtime.get('instanceId') != config['instance_id'] or runtime.get('workspaceId') != config['workspace_id']:
        raise ValueError('RunDesk 未返回 Docker 环境编号，未写入工具配置')
    initialized = environment_id in config.get('docker_initialized_environments', [])
    params = {'instanceId': config['instance_id']}
    mcp = client.request('GET', '/workspaces/' + wid + '/mcp', params=params)
    servers = {**(mcp.get('effectiveServers') or {}), **(mcp.get('userServers') or {})}
    own = servers.get('news2douyin-research')
    if own is None:
        if initialized:
            raise ValueError('已配置环境的研究 MCP 已被删除；请在 RunDesk 核对，重连不会自动恢复')
        client.request('PUT', '/workspaces/' + wid + '/mcp/news2douyin-research', {
            'version': mcp['version'], 'config': {'url': callback + '/mcp/research', 'enabled': True,
                'http_headers': {'Authorization': 'Bearer ' + config['mcp_token']}, 'tool_timeout_sec': 90}}, params=params)
    elif own.get('enabled') is False or own.get('url') != callback + '/mcp/research':
        raise ValueError('容器内已有研究 MCP 被关闭或地址不同；请在 RunDesk 核对后修改，连接操作不会覆盖管理员配置')
    configure_exa(client, config)
    path = '/workspaces/' + wid + '/skills/news-research'
    skill_params = {**params, 'scope': 'instance'}
    try:
        client.request('GET', '/workspaces/' + wid + '/skill-bundles/news-research', params=skill_params)
    except RemoteError as exc:
        if exc.status != 404:
            raise
        if initialized:
            raise ValueError('已配置环境的研究 Skill 已被删除；请在 RunDesk 核对，重连不会自动恢复') from None
        client.request('PUT', path, {'content': skill}, params=skill_params)
    if config.get('docker_permissions') == 'full-access':
        configure_docker_full_access(client, config)
    config['docker_initialized_environments'] = sorted(set(config.get('docker_initialized_environments', [])) | {environment_id})
    permission_message = ('Codex 完整访问与当前已启用 MCP 的持续授权已配置。已有等待审批的研究请停止后重新开始。'
                          if config.get('docker_permissions') == 'full-access' else '沿用 RunDesk 中的权限设置。')
    config.update(connection_status='connected', setup_message='Docker 环境与业务工具已配置。' + permission_message + '首次使用请在 RunDesk 完成此环境的模型认证；开始研究时自动检查认证与检索工具。',
                  environment_id=environment_id)
    settings.write(config)


def configure_exa(client, config):
    """Explicit setup selects Exa, retaining custom endpoints and credentials."""
    params = {'instanceId': config['instance_id']}
    path = '/workspaces/' + quote(config['workspace_id'], safe='') + '/mcp'
    for name in ('exa', 'wigolo'):
        snapshot = client.request('GET', path, params=params)
        servers = {**(snapshot.get('effectiveServers') or {}), **(snapshot.get('userServers') or {})}
        old = servers.get(name)
        if name == 'wigolo':
            if not isinstance(old, dict) or old.get('enabled') is False:
                continue
            value = {**deepcopy(old), 'enabled': False}
        else:
            value = deepcopy(old) if isinstance(old, dict) else {
                'url': 'https://mcp.exa.ai/mcp?tools=web_search_exa,web_fetch_exa,web_search_advanced_exa',
                'startup_timeout_sec': 30, 'tool_timeout_sec': 90,
                'enabled_tools': ['web_search_exa', 'web_fetch_exa', 'web_search_advanced_exa']}
            value['enabled'] = True
        if value != old:
            client.request('PUT', path + '/' + name, {'version': snapshot['version'], 'config': value}, params=params)


def configure_docker_full_access(client, config):
    """Explicit setup only. Native policies persist in this environment's CODEX_HOME.

    Keep server/tool enablement, credentials and result limits. We do not answer
    elicitation forms or authorize a tool call on the application's behalf.
    """
    iid, wid = quote(config['instance_id'], safe=''), quote(config['workspace_id'], safe='')
    params = {'instanceId': config['instance_id']}
    def docker_instance():
        instance = client.request('GET', '/instances/' + iid + '/configuration', params={'workspaceId': config['workspace_id']})['instance']
        if (instance.get('execution') or {}).get('mode') != 'docker' or ((instance.get('agentRuntime') or {}).get('kind') or 'codex') != 'codex':
            raise ValueError('实例已不再是 Codex Docker，未放宽运行权限')
        return instance

    docker_instance()
    path = '/workspaces/' + wid + '/mcp'
    snapshot = client.request('GET', path, params=params)
    names = sorted({**(snapshot.get('effectiveServers') or {}), **(snapshot.get('userServers') or {})})
    for name in names:
        # A save changes the whole config version; always use a fresh snapshot.
        docker_instance()
        snapshot = client.request('GET', path, params=params)
        servers = {**(snapshot.get('effectiveServers') or {}), **(snapshot.get('userServers') or {})}
        current = servers.get(name)
        if not isinstance(current, dict) or current.get('enabled') is False:
            continue
        policy = deepcopy(current)
        policy['default_tools_approval_mode'] = 'approve'
        for tool in (policy.get('tools') or {}).values():
            tool['approval_mode'] = 'approve'
        if policy != current:
            client.request('PUT', path + '/' + quote(name, safe=''), {'version': snapshot['version'], 'config': policy}, params=params)
    instance = docker_instance()
    permissions = {'sandbox': 'danger-full-access', 'approvalPolicy': 'never', 'reviewer': 'user'}
    if instance.get('permissions') != permissions:
        client.request('PATCH', '/instances/' + iid, {'revision': instance['revision'], 'permissions': permissions})


RESEARCH_TOOLS = {'research_get_case', 'research_search', 'research_record_search', 'research_read_source', 'research_save_question', 'research_save_claim', 'research_save_report', 'research_save_short_script'} | {'topic_get_workspace','topic_search_library','topic_read_article','topic_save_candidates','topic_save_reply'}


def validate_library_tools(servers):
    from ..topics.tools import NAMES
    if not NAMES <= set(servers.get('news2douyin-research', [])):
        raise ValueError('未发现完整选题工具，请更新 news2douyin 并刷新 RunDesk 中的 MCP 工具列表')


def validate_search_tools(servers, config):
    from .config import require_search_route
    require_search_route(config['search_provider'], config['search_backend'])
    if not RESEARCH_TOOLS <= set(servers.get('news2douyin-research', [])):
        raise ValueError('未发现完整研究工具；请检查 MCP 回调地址和 news2douyin 版本。容器中的 localhost 不是宿主机')
    if 'web_search_exa' not in servers.get('exa', []):
        raise ValueError('未发现 Exa 搜索工具，请在 RunDesk 启用 Exa MCP 并检查网络；不会回退到新闻采集的搜索服务')


def validate_runtime(value, config, *, library_only=False):
    if not isinstance(value, dict) or value.get('instanceId') != config['instance_id'] or value.get('workspaceId') != config['workspace_id']:
        raise ValueError('容器回执与研究应用绑定不一致')
    env = value.get('environment') or {}
    if value.get('executionMode') != 'docker' or env.get('state') != 'running' or not env.get('id') or env.get('pendingUpdate'):
        raise ValueError('Docker 环境尚未就绪或等待更新，请在 RunDesk 检查')
    if not value.get('probed') or value.get('errors'):
        raise ValueError('容器配置探测未通过，请检查 Codex 和 MCP 服务')
    if not value.get('accountReady'):
        raise ValueError('Docker 环境尚未配置模型认证，请在 RunDesk 此应用的模型与认证页面完成配置')
    servers = value.get('servers') or {}
    validate_library_tools(servers) if library_only else validate_search_tools(servers, config)
    return env


def local_runtime_status(client, config, *, library_only=False):
    path = '/workspaces/' + quote(config['workspace_id'], safe='')
    account = client.request('GET', path + '/account', params={'instanceId': config['instance_id']})
    overview = client.request('GET', '/instances/' + quote(config['instance_id'], safe='') + '/configuration',
                              params={'workspaceId': config['workspace_id'], 'probe': '1'})
    state = overview.get('mcp', {}).get('status') or {}
    entries = state.get('data', []) if isinstance(state, dict) else state if isinstance(state, list) else []
    servers = {}
    for entry in entries:
        tools = entry.get('tools') or {}
        servers[entry.get('name')] = sorted(tools) if isinstance(tools, dict) else [x.get('name', '') for x in tools]
    result = {'account_ready': bool(account.get('account')) or account.get('requiresOpenaiAuth') is False,
              'default_model': overview.get('instance', {}).get('defaultModel', ''),
              'skills': [{'name': v.get('name'), 'scope': v.get('sourceScope'), 'enabled': v.get('enabled')} for v in overview.get('skills', [])],
              'configuration_errors': overview.get('errors', {}),
              'tools': servers.get('news2douyin-research', []),
              'mcp_ready': RESEARCH_TOOLS <= set(servers.get('news2douyin-research', [])),
              'search_ready': 'web_search_exa' in servers.get('exa', [])}
    try:
        if not result['account_ready']:
            raise ValueError('尚未配置模型认证，请在 RunDesk 完成认证')
        if result['configuration_errors']:
            raise ValueError('RunDesk 配置探测未通过，请检查模型和 MCP 配置')
        validate_library_tools(servers) if library_only else validate_search_tools(servers, config)
        result.update(ready=True, message='模型认证、研究工具和 Exa 检查完成；尚未验证真实研究质量')
    except ValueError as exc:
        result.update(ready=False, message=str(exc))
    return result


def connection_check(settings, client_factory=RunDeskClient):
    config = settings.get()
    client = client_factory(config)
    meta = require_v1(client)
    result = {'version': meta.get('version'), 'demo': meta.get('demo'), 'configured': bool(config['instance_id'] and config['workspace_id'])}
    if result['configured'] and config.get('runtime_mode') == 'docker':
        if 'application-runtime' not in meta.get('capabilities', []):
            raise ValueError('一键 Docker 研究需要 RunDesk 0.47.0 或更新版本')
        runtime = prepare_runtime(client, config)
        result.update(runtime=runtime, account_ready=runtime.get('accountReady', False),
                      mcp_ready=RESEARCH_TOOLS <= set((runtime.get('servers') or {}).get('news2douyin-research', [])))
        try:
            validate_runtime(runtime, config)
            result.update(ready=True, message='Docker、模型认证与研究工具检查通过；可以开始研究，质量仍需核验。')
        except ValueError as exc:
            result.update(ready=False, message=str(exc))
        return result
    if result['configured']:
        result.update(local_runtime_status(client, config))
    return result
