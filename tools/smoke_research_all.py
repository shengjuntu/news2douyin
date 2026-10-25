"""Run fixture, official MCP client, and browser checks in one local network scope."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

import requests


def main(output):
    root = Path(__file__).resolve().parents[1]
    output = Path(output).resolve(); output.mkdir(parents=True,exist_ok=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix='news-research-smoke-') as state:
        base=f'http://127.0.0.1:{port}'
        with (output/'fixture.log').open('w') as log:
            server=subprocess.Popen([sys.executable,str(root/'tools/smoke_research_server.py'),'--root',state,'--port',str(port)],stdout=log,stderr=log,cwd=root)
            try:
                for _ in range(100):
                    try:
                        if requests.get(base+'/api/health',timeout=.5).ok:break
                    except requests.RequestException:pass
                    if server.poll() is not None:raise RuntimeError('fixture stopped')
                    time.sleep(.1)
                mcp=subprocess.run([sys.executable,str(root/'tools/smoke_research_mcp.py'),'--base',base,'--settings',str(Path(state)/'research/settings.json')],capture_output=True,text=True,timeout=45)
                (output/'mcp-client.log').write_text(mcp.stdout+mcp.stderr)
                if mcp.returncode:raise RuntimeError(mcp.stdout+mcp.stderr)
                print(mcp.stdout.strip(),flush=True)
                node=os.environ['CODEX_PRIMARY_RUNTIME_NODE']
                browser=subprocess.run([node,str(root/'tools/smoke_research_browser.cjs'),base,str(output)],capture_output=True,text=True,timeout=120)
                (output/'browser.log').write_text(browser.stdout+browser.stderr)
                if browser.returncode:raise RuntimeError(browser.stdout+browser.stderr)
                print(browser.stdout.strip(),flush=True)
            finally:
                server.terminate()
                try:server.wait(timeout=5)
                except subprocess.TimeoutExpired:server.kill();server.wait()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);main(p.parse_args().output)
