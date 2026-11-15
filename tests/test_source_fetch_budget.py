"""A single public-source budget includes DNS, redirects, headers and body.

Only the connection transport is mapped to a real loopback HTTP fixture; URL
validation still sees public hostnames and pinned public IPs. No outside request
or model is contacted.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import gzip
import socket
import ssl
import shutil
import subprocess
from threading import Event,Thread
import time

import pytest

from news2douyin.research import sources

HOST='budget-fixture.example'
PUBLIC='93.184.216.34'


@contextmanager
def transport(monkeypatch,tls=None):
    state={'calls':[], 'connections':[]}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            state['calls'].append(self.path)
            try:
                if self.path=='/slow-headers':
                    self.wfile.write(b'HTTP/1.1 200 OK\r\nX-Slow: ');self.wfile.flush()
                    for _ in range(100):
                        self.wfile.write(b'x');self.wfile.flush();time.sleep(.03)
                    self.wfile.write(b'\r\nContent-Length: 1\r\n\r\nx');return
                if self.path=='/slow-body':
                    self.send_response(200);self.send_header('Content-Type','text/html')
                    self.send_header('Content-Length','100000');self.end_headers()
                    for _ in range(100000):
                        self.wfile.write(b'x');self.wfile.flush();time.sleep(.03)
                    return
                if self.path.startswith('/delayed-redirect/'):
                    number=int(self.path.rsplit('/',1)[1]);time.sleep(.08)
                    self.send_response(302);self.send_header('Location','/delayed-redirect/'+str(number-1) if number else '/ok')
                    self.send_header('Content-Length','0');self.end_headers();return
                if self.path=='/redirect':
                    self.send_response(302);self.send_header('Location','/slow-body')
                    self.send_header('Content-Length','0');self.end_headers();return
                if self.path=='/private':
                    self.send_response(302);self.send_header('Location','http://127.0.0.1/admin')
                    self.send_header('Content-Length','0');self.end_headers();return
                if self.path=='/loop':
                    self.send_response(302);self.send_header('Location','/loop')
                    self.send_header('Content-Length','0');self.end_headers();return
                body=(b'<html><title>A real fixture headline</title><p>Source excerpt.</p></html>'
                      if self.path not in ('/large','/gzip-large') else b'x'*10000)
                compressed=self.path in ('/gzip','/gzip-large')
                if compressed:body=gzip.compress(body)
                self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8')
                if compressed:self.send_header('Content-Encoding','gzip')
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError,OSError):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    if tls is not None:
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(tls[0]),str(tls[1]))
        server.socket=context.wrap_socket(server.socket,server_side=True)
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    original_resolve=sources.socket.getaddrinfo
    original_pool=sources.urllib3.HTTPConnectionPool
    original_https=sources.urllib3.HTTPSConnectionPool
    def resolve(host,port,*args,**kwargs):
        if host==HOST:return [(socket.AF_INET,socket.SOCK_STREAM,6,'',(PUBLIC,port))]
        return original_resolve(host,port,*args,**kwargs)
    def pool(host,port=None,**kwargs):
        assert host==PUBLIC and port==80
        result=original_pool('127.0.0.1',port=server.server_port,**kwargs)
        state['connections'].append(result)
        return result
    def https_pool(host,port=None,**kwargs):
        assert tls is not None and host==PUBLIC and port==443
        assert kwargs['server_hostname']==kwargs['assert_hostname']==HOST
        assert kwargs['cert_reqs']==ssl.CERT_REQUIRED
        kwargs['ca_certs']=str(tls[0])
        result=original_https('127.0.0.1',port=server.server_port,**kwargs)
        state['connections'].append(result)
        return result
    monkeypatch.setattr(sources.socket,'getaddrinfo',resolve)
    monkeypatch.setattr(sources.urllib3,'HTTPConnectionPool',pool)
    if tls is not None:monkeypatch.setattr(sources.urllib3,'HTTPSConnectionPool',https_pool)
    try:yield state
    finally:server.shutdown();server.server_close();thread.join(timeout=2)


@pytest.mark.parametrize('path',['/slow-body','/slow-headers','/redirect'])
def test_absolute_deadline_interrupts_real_trickling_body_and_headers(monkeypatch,path):
    with transport(monkeypatch) as state:
        started=time.monotonic()
        with pytest.raises(ValueError,match='总时间预算'):
            sources.safe_fetch('http://'+HOST+path,timeout_seconds=.2,max_bytes=1024)
        elapsed=time.monotonic()-started
        assert .15<=elapsed<1,elapsed
        assert state['calls'] and all(pool.pool is None for pool in state['connections'])


def test_timed_dns_returns_without_join_and_caps_unfinished_lookups(monkeypatch):
    release=Event();entered=[]
    original_slots=sources._DNS_SLOTS
    def stuck(host,port,**kwargs):
        entered.append(host);release.wait(2)
        return [(socket.AF_INET,socket.SOCK_STREAM,6,'',(PUBLIC,port))]
    monkeypatch.setattr(sources.socket,'getaddrinfo',stuck)
    try:
        for _ in range(2):
            started=time.monotonic()
            with pytest.raises(ValueError,match='总时间预算'):
                sources.safe_fetch('http://'+HOST,timeout_seconds=.04)
            assert time.monotonic()-started<.5
        with pytest.raises(ValueError,match='解析繁忙'):
            sources.safe_fetch('http://'+HOST,timeout_seconds=.04)
        assert len(entered)==2
    finally:
        release.set()
        # Restore capacity deterministically before another parallel fixture
        # reuses this module. The daemon workers release their original slots.
        for _ in range(100):
            if original_slots._value==2:break
            time.sleep(.005)
        assert original_slots._value==2


@pytest.mark.parametrize('budget',[None,1])
def test_existing_default_and_budget_paths_keep_real_html_and_gzip(monkeypatch,budget):
    with transport(monkeypatch):
        for path in ('/ok','/gzip'):
            kwargs={} if budget is None else dict(timeout_seconds=budget,max_bytes=1024)
            url,mime,body=sources.safe_fetch('http://'+HOST+path,**kwargs)
            assert url.endswith(path) and mime.startswith('text/html')
            assert b'A real fixture headline' in body


@pytest.mark.parametrize('path',['/large','/gzip-large'])
def test_preview_limit_is_decoded_response_bytes(monkeypatch,path):
    with transport(monkeypatch):
        with pytest.raises(ValueError,match='字节上限'):
            sources.safe_fetch('http://'+HOST+path,timeout_seconds=1,max_bytes=100)


def test_redirect_resolves_and_blocks_private_target_under_budget(monkeypatch):
    with transport(monkeypatch) as state:
        with pytest.raises(ValueError,match='内网'):
            sources.safe_fetch('http://'+HOST+'/private',timeout_seconds=1)
        assert state['calls']==['/private']


def test_redirect_ceiling_is_shared_not_restarted(monkeypatch):
    with transport(monkeypatch) as state:
        with pytest.raises(ValueError,match='重定向次数'):
            sources.safe_fetch('http://'+HOST+'/loop',timeout_seconds=2)
        assert state['calls']==['/loop']*5


@pytest.mark.parametrize('url',['http://alice:password@'+HOST+'/secret',
    'http://'+HOST+':8888/', 'file:///private', 'http://127.0.0.1/private'])
def test_budget_preserves_public_standard_port_and_credential_guards(monkeypatch,url):
    monkeypatch.setattr(sources.socket,'getaddrinfo',lambda *a,**k:[(2,1,6,'',('127.0.0.1',80))])
    with pytest.raises(ValueError) as error:sources.safe_fetch(url,timeout_seconds=1)
    assert 'password' not in str(error.value) and 'alice:' not in str(error.value)


@pytest.mark.parametrize('kwargs',[dict(timeout_seconds=0),dict(timeout_seconds=float('inf')),
    dict(timeout_seconds=True),dict(max_bytes=0),dict(max_bytes=True),dict(max_bytes=sources.MAX_BYTES+1)])
def test_invalid_budgets_fail_before_network(monkeypatch,kwargs):
    def forbidden(*args,**kw):raise AssertionError('Network was touched')
    monkeypatch.setattr(sources.socket,'getaddrinfo',forbidden)
    with pytest.raises(ValueError):sources.safe_fetch('http://'+HOST,**kwargs)


def test_redirect_hops_do_not_restart_the_absolute_budget(monkeypatch):
    with transport(monkeypatch) as state:
        started=time.monotonic()
        with pytest.raises(ValueError,match='总时间预算'):
            sources.safe_fetch('http://'+HOST+'/delayed-redirect/3',timeout_seconds=.2)
        assert time.monotonic()-started<1
        assert 1<=len(state['calls'])<=3


@pytest.mark.skipif(shutil.which('openssl') is None,reason='Local TLS budget fixture needs openssl')
def test_https_pinned_host_verification_and_detached_socket_deadline(monkeypatch,tmp_path):
    cert,key=tmp_path/'cert.pem',tmp_path/'key.pem'
    config=tmp_path/'openssl.cnf'
    config.write_text('[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN='+HOST+'\n[ext]\nsubjectAltName=DNS:'+HOST+'\n')
    subprocess.run([shutil.which('openssl'),'req','-x509','-newkey','rsa:2048',
        '-keyout',str(key),'-out',str(cert),'-days','1','-nodes','-config',str(config)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    with transport(monkeypatch,tls=(cert,key)) as state:
        final,mime,body=sources.safe_fetch('https://'+HOST+'/ok',timeout_seconds=2,max_bytes=1024)
        assert final=='https://'+HOST+'/ok' and b'A real fixture headline' in body
        started=time.monotonic()
        with pytest.raises(ValueError,match='总时间预算'):
            sources.safe_fetch('https://'+HOST+'/slow-body',timeout_seconds=.25,max_bytes=1024)
        assert time.monotonic()-started<1.5
        assert state['calls']==['/ok','/slow-body']
        assert all(pool.pool is None for pool in state['connections'])
