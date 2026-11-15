"""The deployment probe distinguishes availability without leaking raw data."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('probe_newsnow', Path(__file__).resolve().parents[1]/'tools/probe_newsnow.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class Unavailable(Exception):
    def __init__(self, batch):
        self.batch = batch


def batch(*, succeeded=1, failed=0, count=1):
    return dict(items=[{'title':'do not output item text'}] * count,
        summary=dict(requested_sources=succeeded+failed, succeeded_sources=succeeded,
            failed_sources=failed, empty_sources=int(not count), raw_items=count,
            valid_items=count, selected_items=count, truncated=False,
            status='partial' if failed else 'complete'),
        sources=[dict(source_id='zhihu', status='success', http_status=200,
            raw_items=count, valid_items=count, error='https://private.example/?token=secret',
            cookies='secret-cookie', raw_payload={'secret':'never echo'})],
        snapshots=[dict(source_id='zhihu', response_status='cache', observed_at='2026-11-14T01:00:00Z',
            snapshot_reported_at='2026-11-14T00:30:00Z', snapshot_time_basis='server_reported',
            payload={'secret':'never echo'})])


@pytest.mark.parametrize('succeeded,failed,count,expected',[(1,0,1,0),(1,1,1,2),(0,2,0,3),(2,0,0,4)])
def test_probe_availability_codes_and_safe_output(succeeded,failed,count,expected):
    data=batch(succeeded=succeeded,failed=failed,count=count)
    def fetch(_config):
        if not succeeded:
            raise Unavailable(data)
        return data
    provider=SimpleNamespace(fetch_batch=fetch, NewsNowUnavailable=Unavailable)
    report,code=probe.probe({'base_url':'http://example.test:4444'},provider=provider)
    assert code==expected==report['exit_code']
    serialized=json.dumps(report)
    assert 'secret' not in serialized and 'do not output' not in serialized
    assert report['sources'][0]['snapshot_reported_at']=='2026-11-14T00:30:00Z'
    assert report['sources'][0]['response_status']=='cache'
    assert '不保证' in report['freshness_note']


def test_cli_uses_only_requested_sources_and_persists_safe_json(tmp_path,monkeypatch,capsys):
    calls=[]
    provider=SimpleNamespace(DEFAULT_SOURCES=['zhihu','cls'],NewsNowUnavailable=Unavailable,
        fetch_batch=lambda config:(calls.append(config) or batch()))
    monkeypatch.setattr(probe,'_provider',lambda:provider)
    output=tmp_path/'probe.json'
    assert probe.main(['--base-url','http://example.test:4444','--sources','zhihu,cls','--output',str(output)])==0
    assert calls[0]['newsnow_sources']==['zhihu','cls']
    assert json.loads(output.read_text())['summary']['succeeded_sources']==1
    assert json.loads(capsys.readouterr().out)['endpoint']=='/api/s?id=<source>'


def test_cli_exception_does_not_echo_credentials_or_exception_body(monkeypatch,capsys):
    def fetch(_config):
        raise ValueError('secret-password https://private.example?token=secret')
    monkeypatch.setattr(probe,'_provider',lambda:SimpleNamespace(DEFAULT_SOURCES=['zhihu'],
        fetch_batch=fetch,NewsNowUnavailable=Unavailable))
    assert probe.main(['--base-url','http://user:secret-password@example.test:4444/?token=secret'])==1
    response=capsys.readouterr().out
    assert 'secret' not in response and 'user:' not in response
    assert json.loads(response)['base_url']=='http://example.test:4444'


def test_real_http_cli_default_custom_sources_and_empty_failure_exit_codes(tmp_path):
    from newsnow_test_support import newsnow_http,snapshot
    script=Path(__file__).resolve().parents[1]/'tools/probe_newsnow.py'
    with newsnow_http() as fixture:
        fixture.set('douyin',snapshot('douyin',[dict(id='hot-1',title='关税热点',
            url='https://www.douyin.com/hot/1')]))
        output=tmp_path/'safe-diagnostics.json'
        default=subprocess.run([sys.executable,'-I',str(script),'--base-url',fixture.base,
            '--output',str(output)],capture_output=True,text=True,timeout=30)
        assert default.returncode==0,default.stdout+default.stderr
        data=json.loads(default.stdout)
        assert fixture.calls==['zhihu','cls','douyin']
        assert data['summary']['succeeded_sources']==3
        assert data['summary']['raw_items']==4
        assert data==json.loads(output.read_text(encoding='utf-8'))
        assert '3761' not in default.stdout and '这是保存的摘要' not in default.stdout
        assert {row['source_id'] for row in data['sources']}=={'zhihu','cls','douyin'}
        for sources,expected in [('fastbull',4),('html',3),('zhihu,broken',2)]:
            result=subprocess.run([sys.executable,'-I',str(script),'--base-url',fixture.base,
                '--sources',sources],capture_output=True,text=True,timeout=30)
            assert result.returncode==expected,result.stdout+result.stderr
            assert json.loads(result.stdout)['exit_code']==expected
