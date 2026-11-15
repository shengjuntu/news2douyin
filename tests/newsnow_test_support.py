"""Disposable HTTP-only NewsNow fixtures based on supplied field shapes.

These records are synthetic. They do not assert that a deployed platform source
is reachable, and they never query the user's private service.
"""
from contextlib import contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Lock, Thread
from urllib.parse import parse_qs, urlsplit

STAMP=1794638758000


def snapshot(source,items,*,stamp=STAMP,status='cache'):
    return dict(status=status,id=source,updatedTime=stamp,items=items)


class NewsNowFixture:
    def __init__(self):
        self.lock=Lock()
        self.calls=[]
        self.responses={
            'zhihu':snapshot('zhihu',[
                dict(id='question-1',title='关税调整将怎样影响普通家庭？',url='https://www.zhihu.com/question/1001',
                     extra=dict(info='3761 万热度',hover='这是保存的摘要线索，尚未读取来源全文。')),
                dict(id='question-2',title='港口运输出现了哪些变化？',url='https://www.zhihu.com/question/1002')]),
            'cls':snapshot('cls',[dict(id=2534448,title='一汽解放发动机通过认证',
                mobileUrl='https://api3.cls.cn/share/article/2534448',pubDate=STAMP,
                url='https://www.cls.cn/detail/2534448')]),
            'wallstreetcn':snapshot('wallstreetcn',[dict(id=3200112,title='微软称产品正常运行',
                extra=dict(date=1794638508000),url='https://wallstreetcn.com/livenews/3200112')]),
            'xueqiu':snapshot('xueqiu',[dict(id='NVDA',title='英伟达',url='https://xueqiu.com/S/NVDA',extra=dict(info='+2.1%'))]),
            'fastbull':snapshot('fastbull',[]),
            'html':(200,'text/html','<!doctype html><title>SPA fallback</title>'),
            'broken':(500,'application/json',dict(error=True,message='fixture source unavailable')),
        }
        self.base=''

    def answer(self,source):
        with self.lock:
            self.calls.append(source)
            value=deepcopy(self.responses.get(source,self.responses['broken']))
        if isinstance(value,tuple):return value
        return 200,'application/json',value

    def set(self,source,value):
        with self.lock:self.responses[source]=deepcopy(value)


@contextmanager
def newsnow_http():
    fixture=NewsNowFixture()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed=urlsplit(self.path)
            if parsed.path!='/api/s':
                status,kind,value=404,'application/json',dict(error=True,message='only /api/s is supported')
            else:
                source=parse_qs(parsed.query).get('id',[''])[0]
                status,kind,value=fixture.answer(source)
            body=(json.dumps(value,ensure_ascii=False) if kind=='application/json' else value).encode('utf-8')
            self.send_response(status);self.send_header('Content-Type',kind+'; charset=utf-8')
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(self,*_args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.daemon_threads=True
    fixture.base='http://127.0.0.1:'+str(server.server_port)
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield fixture
    finally:server.shutdown();server.server_close();thread.join(timeout=2)
