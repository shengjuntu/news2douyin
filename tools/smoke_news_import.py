"""Verify a versioned-source script ZIP against an existing Go Video App binary."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import time
import urllib.request
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', required=True, type=Path)
    parser.add_argument('--script', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(24)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def request(path, data=None, content_type=None):
        headers = {'Authorization':'Bearer '+token}
        if content_type:
            headers['Content-Type'] = content_type
        with opener.open(urllib.request.Request(base+path, data=data, headers=headers), timeout=10) as response:
            return response.status, json.load(response)
    with zipfile.ZipFile(args.script) as archive:
        snapshot_bytes = archive.read('revision.json')
        snapshot = json.loads(snapshot_bytes)
        assert all(s.get('article_revision') and s.get('article_content_hash') for s in snapshot['sources'])
    boundary = 'news-'+secrets.token_hex(8)
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="script.zip"\r\nContent-Type: application/zip\r\n\r\n'.encode()
            + args.script.read_bytes() + f'\r\n--{boundary}--\r\n'.encode())
    with (root/'app.log').open('w') as log:
        process = subprocess.Popen([str(args.app.resolve()), '--listen', f'127.0.0.1:{port}', '--data', str(root/'app')],
                                   env=dict(os.environ, VIDEO_APP_TOKEN=token), stdout=log, stderr=log)
        try:
            for _ in range(100):
                try:
                    request('/api/projects'); break
                except OSError:
                    if process.poll() is not None:
                        raise RuntimeError('Video App exited; inspect app.log')
                    time.sleep(.1)
            status, project = request('/api/news/import', body, 'multipart/form-data; boundary='+boundary)
            assert status == 201
            assert ''.join(s['text'] for s in project['shots']) == snapshot['document']['script_text']
            assert project['news']['contentHash'] == hashlib.sha256(snapshot_bytes).hexdigest()
            status, again = request('/api/news/import', body, 'multipart/form-data; boundary='+boundary)
            assert status == 200 and again['id'] == project['id']
            report = {'passed':4, 'checks':['versioned_sources_accepted','exact_narration','snapshot_hash','idempotent_import'],
                      'scope':'Go Video App import only; no real Codex or rendering in this check'}
            (root/'report.json').write_text(json.dumps(report,indent=2)+'\n')
            print(json.dumps(report))
        finally:
            process.terminate()
            process.wait(timeout=10)


if __name__ == '__main__':
    main()
