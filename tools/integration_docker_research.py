"""Two real HTTP services; Docker CLI/Codex/search are explicitly fixtures.

Exercises registration, Docker selection/start, MCP callback transport, durable
research evidence, duplicate start and cancellation. Never a live-model test.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

import requests
import uvicorn


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def wait(fn, seconds=30):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        try:
            result = fn()
            if result: return result
        except requests.RequestException:
            pass
        time.sleep(.1)
    raise AssertionError('Timed out waiting for fixture')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rundesk-root', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    rdroot = Path(args.rundesk_root).resolve()
    out = Path(args.output).resolve(); out.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    from news2douyin.server.app import create_app
    from news2douyin.research.rundesk import RunDeskClient
    from news2douyin.storage.models import Article
    from sqlmodel import Session
    checks = []
    with tempfile.TemporaryDirectory(prefix='news-docker-wire-') as tmp:
        root = Path(tmp); cli = root/'cli'; cli.mkdir(); engine = root/'docker'; engine.mkdir()
        shutil.copyfile(rdroot/'scripts/fixtures/docker-engine.py', cli/'docker'); (cli/'docker').chmod(0o755)
        rdurl, newsurl = 'http://127.0.0.1:'+str(port()), 'http://127.0.0.1:'+str(port())
        token = 'integration-only-admin-not-a-real-secret'
        env = {**os.environ, 'PATH': str(cli)+os.pathsep+os.environ['PATH'], 'DOCKER_CONFIG': str(engine), 'RUNDESK_TOKEN': token}
        log = (out/'rundesk.log').open('w')
        process = subprocess.Popen([str(rdroot/'bin/rundesk'), '--demo', '--data', str(root/'rundesk'), '--listen', rdurl.split('//')[1]], env=env, stdout=log, stderr=subprocess.STDOUT)
        server = None; thread = None
        def rd(method, path, data=None, *, key=None):
            response = requests.request(method, rdurl+'/api/v1'+path, headers={'Authorization':'Bearer '+token, **({'Idempotency-Key':key} if key else {})}, json=data, timeout=30)
            assert response.ok, (path,response.status_code,response.text)
            return response.json()
        try:
            wait(lambda: requests.get(rdurl+'/api/v1/meta',timeout=.5).status_code == 401)
            app = create_app(db_url='sqlite:///'+str(root/'news.db'), storage_root=str(root/'news'))
            class FixtureClient(RunDeskClient):
                def request(self, method, path, *a, **kw):
                    value = super().request(method, path, *a, **kw)
                    # Test-only adaptations: DemoAgent has no real MCP search.
                    # The production client never bypasses these checks.
                    if path == '/meta': value['demo'] = False
                    if path.endswith('/runtime/prepare') and value.get('probed'):
                        config = app.state.research_settings.get()
                        mcp = rd('GET','/workspaces/'+config['workspace_id']+'/mcp?instanceId='+config['instance_id'])
                        own = (mcp.get('effectiveServers') or {}).get('news2douyin-research', {})
                        names = []
                        if own.get('enabled') is not False and own.get('url'):
                            for method_name in ['initialize','tools/list']:
                                response = requests.post(own['url'], headers={'Authorization':'Bearer '+config['mcp_token']},
                                    json={'jsonrpc':'2.0','id':1,'method':method_name,'params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'wire-fixture','version':'1'}} if method_name=='initialize' else {}}, timeout=10)
                                assert response.ok, response.text
                                payload = response.json()['result']
                                if method_name == 'tools/list': names = [x['name'] for x in payload['tools']]
                        value['servers'] = {'news2douyin-research': names, 'exa': ['web_search_exa']}
                    return value
            app.state.research.client_factory = FixtureClient
            with Session(app.state.engine) as s:
                s.add(Article(article_key='wire-fixture',title='协议联调样例新闻',content='旧政策于 2023 年实施。',url='https://example.com/policy',published_at='2026-01-01T00:00:00Z'));s.commit()
            server = uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=int(newsurl.rsplit(':',1)[1]),log_level='warning'))
            thread = threading.Thread(target=server.run,daemon=True);thread.start()
            wait(lambda: requests.get(newsurl+'/api/research/settings',timeout=.5).status_code==200)
            def news(method,path,data=None):
                response=requests.request(method,newsurl+path,json=data,timeout=60)
                assert response.ok,(path,response.status_code,response.text)
                return response.json()
            news('PUT','/api/research/settings',{'rundesk_url':rdurl,'rundesk_token':token,'callback_url':newsurl,
                'mcp_callback_url':newsurl,'runtime_mode':'docker','docker_image':'news:fixture','search_provider':'rundesk','search_backend':'exa'})
            configured=news('POST','/api/research/setup',{})
            assert configured['environment_id']; checks.append('real_http_registration_and_container_configuration')
            iid, wid = configured['instance_id'], configured['workspace_id']
            overview = rd('GET', '/instances/'+iid+'/configuration?workspaceId='+wid)
            assert overview['instance']['permissions'] == {'sandbox':'danger-full-access','approvalPolicy':'never','reviewer':'user'}
            cp = '/workspaces/'+wid+'/mcp?instanceId='+iid
            saved = rd('GET', cp)
            own = saved['userServers']['news2douyin-research']
            assert own['default_tools_approval_mode'] == 'approve'
            # Emulate the old deployment, retaining the masked saved credential.
            own.pop('default_tools_approval_mode')
            own['tools'] = {'research_read_source': {'approval_mode':'prompt','output_token_limit':1200}}
            rd('PUT', '/workspaces/'+wid+'/mcp/news2douyin-research?instanceId='+iid, {'version':saved['version'],'config':own})
            rd('PATCH', '/instances/'+iid, {'revision':overview['instance']['revision'],
                'permissions':{'sandbox':'workspace-write','approvalPolicy':'on-request','reviewer':'user'}})
            news('POST','/api/research/setup',{})
            upgraded = rd('GET', cp)['userServers']['news2douyin-research']
            assert upgraded['default_tools_approval_mode'] == 'approve'
            assert upgraded['tools']['research_read_source'] == {'approval_mode':'approve','output_token_limit':1200}
            assert rd('GET','/instances/'+iid+'/configuration?workspaceId='+wid)['instance']['permissions']['approvalPolicy'] == 'never'
            other = rd('POST','/instances',{'name':'Separate host instance'},key='host-instance-fixture')
            assert other['permissions']['sandbox'] == 'workspace-write' and other['permissions']['approvalPolicy'] == 'on-request'
            checks.append('native_permission_and_mcp_config_upgrade_preserves_secret_and_instance_scope')
            assert news('POST','/api/research/check',{})['ready']; checks.append('real_business_mcp_initialize_and_tools_list')
            case=news('POST','/api/research/cases',{'article_keys':['wire-fixture'],'goal':'核对政策背景','strategy_id':'background'})
            base='/api/research/cases/'+case['case_id']
            run=news('POST',base+'/start',{'instruction':'审批：仅执行协议联调'})
            assert news('POST',base+'/start',{})['run_id']==run['run_id']
            current=wait(lambda: next((r for r in news('GET',base)['runs'] if r['run_id']==run['run_id'] and r['status'] in ['running','waiting']),None))
            remote=rd('GET','/sessions/'+current['session_id'])
            assert remote['executionMode']=='docker' and remote['environmentId']==configured['environment_id']
            assert remote['source']['taskId']==run['run_id']; checks.append('one_click_bound_docker_session_and_duplicate_start')
            secret=app.state.research_settings.get()['mcp_token']
            def tool(name,arguments):
                response=requests.post(newsurl+'/mcp/research',headers={'Authorization':'Bearer '+secret},json={'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':name,'arguments':{'run_id':run['run_id'],**arguments}}},timeout=10)
                result=response.json()['result'];assert not result['isError'],result
                return json.loads(result['content'][0]['text'])
            detail=tool('research_get_case',{});source=detail['sources'][0]['source_id']
            ticket=tool('research_search',{'query':'fixture policy'})
            assert tool('research_search',{'query':' fixture policy ','scope':'web'}) == ticket
            assert ticket['server']=='exa' and ticket['execution_owner']=='codex' and ticket['server_side_search_performed'] is False
            recorded=tool('research_record_search',{'ticket':ticket['ticket'],'results':[{'title':'Fixture policy','url':'https://example.com/policy','snippet':'Fixture search only'}]})
            assert tool('research_search',{'query':'fixture policy'}) == recorded and not recorded['needs_external_call']
            tool('research_search',{'query':'政策','scope':'library'})
            assert next(r for r in news('GET',base)['runs'] if r['run_id']==run['run_id'])['search_count']==1
            checks.append('single_external_search_ticket_reused_and_library_uses_no_external_budget')
            claim=tool('research_save_claim',{'text':'旧政策于 2023 年实施。','kind':'fact','evidence':[{'source_id':source,'paragraph_id':'P1','quote':'旧政策于 2023 年实施。'}]})
            report=tool('research_save_report',{'title':'协议联调报告','sections':[{'heading':'背景','body':'旧政策于 2023 年实施。','claim_ids':[claim['claim_id']]}],'gaps':['没有执行真实搜索或模型推理'],'completeness':'partial'})
            detail=news('GET',base);assert detail['reports'][0]['report_id']==report['report_id'];checks.append('real_mcp_ticket_evidence_and_report_persistence')
            stopped=news('POST',base+'/stop',{});assert stopped['status'] in ['paused','stopping']
            wait(lambda:news('POST',base+'/sync',{}).get('status')=='paused')
            events=rd('GET','/sessions/'+current['session_id']+'/events?limit=1000')
            assert any(x['method']=='run/input' for x in events);checks.append('run_trace_and_guarded_stop')
            short_payload={'article_keys':['wire-fixture'],'output_language':'en','duration_sec':90,
                           'find_images':False,'idempotency_key':'wire-short-script'}
            brief=news('POST','/api/short-scripts',short_payload)
            assert news('POST','/api/short-scripts',short_payload)['case_id']==brief['case_id']
            short_base='/api/research/cases/'+brief['case_id']
            short_run=wait(lambda:next((r for r in news('GET',short_base)['runs'] if r['status'] in ['running','waiting']),None))
            short_case=tool('research_get_case',{'run_id':short_run['run_id']})
            short_claim=tool('research_save_claim',{'run_id':short_run['run_id'],'text':'旧政策于 2023 年实施。','kind':'fact',
                'evidence':[{'source_id':short_case['sources'][0]['source_id'],'paragraph_id':'P1','quote':'旧政策于 2023 年实施。'}]})
            short_args={'run_id':short_run['run_id'],'title':'Policy background','segments':[{'kind':'fact',
                'text':'The previous policy took effect in 2023.','claim_ids':[short_claim['claim_id']]}],
                'gaps':['Fixture writing only; model quality has not been tested.']}
            saved_short=tool('research_save_short_script',short_args)
            assert tool('research_save_short_script',short_args)==saved_short
            script=news('GET','/api/scripts/'+saved_short['package_key'])
            assert script['document']['output_language']=='en' and script['document']['target_duration_sec']==90
            assert script['sources'][0]['excerpt']=='旧政策于 2023 年实施。'
            assert news('GET','/api/short-scripts/'+brief['case_id'])['package_key']==saved_short['package_key']
            news('POST',short_base+'/stop',{})
            checks.append('short_material_pass_reuses_runtime_and_saves_language_bound_cited_draft')
            calls=[json.loads(line) for line in (engine/'calls.jsonl').read_text().splitlines()]
            assert sum(x[:2]==['container','create'] for x in calls)==1
            assert sum(x[:2]==['container','start'] for x in calls)==1
            checks.append('one_environment_reused_across_setup_check_and_research')
            result={'passed':True,'checks':checks,'scope':'Actual RunDesk/news2douyin HTTP and business MCP; fixture Docker CLI, Codex and search; no real container/model/search quality','dockerCreates':1,'dockerStarts':1}
            (out/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps(result,ensure_ascii=False))
        finally:
            if server: server.should_exit=True
            if thread: thread.join(timeout=5)
            process.terminate()
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired: process.kill();process.wait()
            log.close()


if __name__=='__main__':main()
