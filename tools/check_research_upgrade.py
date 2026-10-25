"""Create a 0.11.0 DB using the released source, open with 0.11.1 and compare rows."""
import argparse,json,os,sqlite3,subprocess,sys,tempfile,zipfile
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--baseline',required=True);p.add_argument('--news',required=True);p.add_argument('--output',required=True);args=p.parse_args();output=Path(args.output).resolve();output.parent.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory() as td:
 root=Path(td)
 with zipfile.ZipFile(Path(args.baseline).resolve()) as z:
  for item in z.infolist():
   if item.filename.startswith('news2douyin/src/') and not item.is_dir():z.extract(item,root)
 old=root/'news2douyin/src';new=Path(args.news).resolve()/'src';db=root/'old.db';store=root/'state'
 setup='''from news2douyin.server.app import create_app
from news2douyin.storage.models import Article
from news2douyin.research.models import ResearchRun,ResearchReport
from news2douyin.research.service import create_case
from sqlmodel import Session
import sys
app=create_app(db_url='sqlite:///'+sys.argv[1],storage_root=sys.argv[2])
with Session(app.state.engine) as s:s.add(Article(article_key='old-news',title='保留新闻',content='原始内容'));s.commit()
case=create_case(app.state.engine,{'article_keys':['old-news'],'goal':'保留课题','strategy_id':'background'})
with Session(app.state.engine) as s:
 s.add(ResearchRun(run_id='old-run',case_id=case['case_id'],status='partial',session_id='original-session'))
 s.add(ResearchReport(report_id='old-report',case_id=case['case_id'],run_id='old-run',version=1,title='旧报告',sections_json='[]',gaps_json='[]',completeness='partial',content_hash='old-hash'))
 s.commit()
app.state.engine.dispose()
'''
 env=dict(os.environ,PYTHONPATH=str(old));subprocess.run([sys.executable,'-c',setup,str(db),str(store)],env=env,check=True,cwd=root,capture_output=True)
 def snapshot():
  with sqlite3.connect(db) as c:
   names=[x[0] for x in c.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'")]
   return {n:c.execute('select * from "'+n+'"').fetchall() for n in names}
 before=snapshot();before.pop('newswritelock',None);before.pop('researchlock',None);assert 'researchsubmission' not in before
 code="from news2douyin.server.app import create_app;import sys;app=create_app(db_url='sqlite:///'+sys.argv[1],storage_root=sys.argv[2]);app.state.engine.dispose()"
 subprocess.run([sys.executable,'-c',code,str(db),str(store)],env=dict(os.environ,PYTHONPATH=str(new)),check=True,cwd=root,capture_output=True)
 after=snapshot();assert 'researchsubmission' in after
 assert all(after[k]==v for k,v in before.items()),[k for k,v in before.items() if after[k]!=v]
 result={'from':'0.11.0','to':'0.11.1','existingTablesCompared':len(before),'allExistingRowsUnchanged':True,'newSubmissionTable':True}
 output.write_text(json.dumps(result,indent=2));print(json.dumps(result))
