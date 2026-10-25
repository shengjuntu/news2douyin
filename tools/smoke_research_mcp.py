"""Interoperability check using the official MCP Python client and local fixture server."""
import argparse
import asyncio
import json
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main(base, settings_path):
    async with httpx.AsyncClient(base_url=base) as http:
        setup = await http.post('/api/research/setup'); setup.raise_for_status()
        token = json.loads(Path(settings_path).read_text())['mcp_token']
        case = (await http.post('/api/research/cases', json={'article_keys':['research-demo'], 'goal':'MCP client interoperability check'})).json()
        run = (await http.post('/api/research/cases/'+case['case_id']+'/start', json={})).json()
        for _ in range(20):
            detail = (await http.get('/api/research/cases/'+case['case_id'])).json()
            if detail['runs'][0]['status']=='running': break
            await asyncio.sleep(.5)
        assert detail['runs'][0]['status']=='running'
        checks=[]
        async with streamablehttp_client(base+'/mcp/research',headers={'Authorization':'Bearer '+token}) as (read,write,_):
            async with ClientSession(read,write) as session:
                initialized=await session.initialize();checks.append('initialize_'+initialized.protocolVersion)
                listed=await session.list_tools();assert len(listed.tools)==6;checks.append('tool_schema_discovery')
                result=await session.call_tool('research_get_case',{'run_id':run['run_id']});assert not result.isError
                data=json.loads(result.content[0].text);assert data['case_id']==case['case_id'];checks.append('case_context')
                source=await session.call_tool('research_read_source',{'run_id':run['run_id'],'source_id':data['sources'][0]['source_id']});assert not source.isError
                assert json.loads(source.content[0].text)['paragraphs'][0]['id']=='P1';checks.append('source_paragraphs')
                report=await session.call_tool('research_save_report',{'run_id':run['run_id'],'title':'Protocol check',
                    'sections':[{'heading':'Limitations','body':'This is a transport check, not a real model study.','claim_ids':[]}],
                    'gaps':['Live Codex research not tested.'],'completeness':'partial'})
                assert not report.isError;checks.append('report_writeback')
                await session.send_ping();checks.append('ping')
        await http.post('/api/research/cases/'+case['case_id']+'/stop')
        print(json.dumps({'passed':len(checks),'checks':checks}))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--base',default='http://127.0.0.1:18311');p.add_argument('--settings',required=True)
    args=p.parse_args();asyncio.run(main(args.base,args.settings))
