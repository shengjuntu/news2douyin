"""Compare persisted 0.14 data and script files across the additive 0.15 upgrade."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--baseline', required=True, help='Unpacked 0.14 source directory')
p.add_argument('--news', required=True, help='Unpacked new source directory')
p.add_argument('--output', required=True)
args = p.parse_args()
with tempfile.TemporaryDirectory() as folder:
    root = Path(folder); db = root / 'upgrade.db'; storage = root / 'runs'
    setup = '''
import sys
from news2douyin.server.app import create_app
from news2douyin.storage.models import Article,Event,ArticleEventLink
from news2douyin.storage.articles import record_version
from news2douyin.editorial.daily import choose,build_pick
from news2douyin.research.service import create_case
from news2douyin.storage.db import init_db
from sqlmodel import Session
app=create_app(db_url='sqlite:///'+sys.argv[1],storage_root=sys.argv[2])
with Session(app.state.engine) as s:
 a=Article(article_key='retained',title='English source',content='The old policy started in 2023.',url='https://example.com/policy')
 s.add_all([a,Event(event_key='policy',event_title='Policy')]);s.flush();record_version(s,a)
 s.add(ArticleEventLink(article_key='retained',event_key='policy'));s.commit()
 pick=choose(s,'retained')[0]
 build_pick(s,pick['selection_key'],storage_root=sys.argv[2])
create_case(app.state.engine,dict(article_keys=['retained'],goal='Retained research'))
init_db(app.state.engine)
app.state.engine.dispose()
'''
    def run(source, code):
        subprocess.run([sys.executable, '-c', code, str(db), str(storage)],
            cwd=root, env={**os.environ, 'PYTHONPATH': str(Path(source).resolve() / 'src')},
            check=True, capture_output=True, text=True)
    run(args.baseline, setup)
    tables = ['article','dailyselection','scriptpackage','scriptstate','scriptrevision',
              'scriptreviewevent','researchcase','researchsource','researchstrategy']
    def state():
        with sqlite3.connect(db) as conn:
            return {t: conn.execute('SELECT * FROM "'+t+'" ORDER BY rowid').fetchall() for t in tables}
    before = state()
    files = {str(f.relative_to(storage)): hashlib.sha256(f.read_bytes()).hexdigest()
             for f in storage.rglob('*') if f.is_file()}
    run(args.news, "from news2douyin.server.app import create_app;import sys;app=create_app(db_url='sqlite:///'+sys.argv[1],storage_root=sys.argv[2]);app.state.engine.dispose()")
    assert state() == before
    assert all(hashlib.sha256((storage / name).read_bytes()).hexdigest() == digest for name, digest in files.items())
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='shortscriptbrief'").fetchone()
    result = dict(passed=True, baseline='0.14.0', upgraded='0.15.0', tables_unchanged=tables,
                  existing_files_unchanged=len(files), additive_table='shortscriptbrief')
    out = Path(args.output); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False))
