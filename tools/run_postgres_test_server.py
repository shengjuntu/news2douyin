"""Run an isolated loopback-only PostgreSQL instance for integration tests.

Install optional QA dependencies: pip install pgserver==0.1.4 'psycopg[binary]'
Preferred usage: python tools/run_postgres_test_server.py -- python -m pytest
tests/test_postgres_integration.py -q. The child command inherits its test URL.
Without a command the server waits until interrupted, and the printed URL can
be used by another terminal sharing its network namespace. No existing database
directory is opened. The temporary cluster and databases are removed on exit.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import threading
import time


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-json',type=Path)
    parser.add_argument('--parent',type=Path,help='Parent for a fresh temporary cluster')
    parser.add_argument('command',nargs=argparse.REMAINDER,
                        help='Optional command after --; inherits the test URL and runs before shutdown')
    args=parser.parse_args()
    import pgserver
    import psycopg
    from psycopg import sql
    binary=Path(pgserver.__file__).parent/'pginstall/bin'
    if not (binary/'postgres').is_file():raise SystemExit('pgserver bundled binaries unavailable')
    if hasattr(os,'geteuid') and os.geteuid()==0:
        raise SystemExit('PostgreSQL requires an unprivileged OS user')
    if args.parent:args.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='n2d-pgqa-',dir=args.parent) as folder:
        root=Path(folder);data=root/'cluster'
        with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
        subprocess.run([str(binary/'initdb'),'-D',str(data),'-U','n2dqa','--auth-local=trust',
                        '--auth-host=trust','--encoding=UTF8','--no-locale'],check=True,
                       stdout=subprocess.DEVNULL)
        log=(root/'postgres.log').open('w')
        child=subprocess.Popen([str(binary/'postgres'),'-D',str(data),'-h','127.0.0.1',
              '-p',str(port),'-k','','-c','max_connections=60',
              '-c','shared_buffers=64MB'],stdout=log,stderr=subprocess.STDOUT)
        stop=threading.Event()
        def stop_server(*_):stop.set()
        for name in ('SIGINT','SIGTERM'):
            if hasattr(signal,name):signal.signal(getattr(signal,name),stop_server)
        try:
            native=f'postgresql://n2dqa@127.0.0.1:{port}/postgres'
            deadline=time.monotonic()+20
            while True:
                try:
                    with psycopg.connect(native,autocommit=True,connect_timeout=1) as connection:
                        version=connection.execute('SELECT version()').fetchone()[0]
                        connection.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier('n2dqa')))
                    break
                except psycopg.OperationalError:
                    if child.poll() is not None or time.monotonic()>=deadline:
                        raise RuntimeError((root/'postgres.log').read_text())
                    stop.wait(.1)
            value=dict(url=f'postgresql+psycopg://n2dqa@127.0.0.1:{port}/n2dqa',
                       native_url=f'postgresql://n2dqa@127.0.0.1:{port}/n2dqa',
                       host='127.0.0.1',port=port,user='n2dqa',database='n2dqa',
                       version=version,root=str(root),postgres_pid=child.pid,
                       qa_only=True)
            if args.output_json:
                args.output_json.parent.mkdir(parents=True,exist_ok=True)
                args.output_json.write_text(json.dumps(value,indent=2))
            print(json.dumps(value),flush=True)
            if args.command:
                command=args.command[1:] if args.command[0]=='--' else args.command
                if not command:raise SystemExit('A command is required after --')
                env=dict(os.environ,NEWS2DOUYIN_TEST_POSTGRES_URL=value['url'])
                raise SystemExit(subprocess.run(command,env=env).returncode)
            print('Set NEWS2DOUYIN_TEST_POSTGRES_URL to url; Ctrl-C stops and removes this temporary server.',flush=True)
            while not stop.wait(1):
                if child.poll() is not None:raise RuntimeError('PostgreSQL exited unexpectedly')
        finally:
            child.terminate()
            try:child.wait(timeout=15)
            except subprocess.TimeoutExpired:child.kill();child.wait()
            log.close()


if __name__=='__main__':main()
