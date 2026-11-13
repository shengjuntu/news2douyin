"""Live app/real DuckDB/FalkorDB; HTTP feed responses are controlled GKG fixtures."""
import hashlib,json,os,sys,subprocess,threading,time,tempfile,sqlite3,shutil
from pathlib import Path
from zipfile import ZipFile
import requests,uvicorn
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'));sys.path.insert(0,str(ROOT/'tools'))
from check_workbench_live import port
from news2douyin.server.app import create_app
from news2douyin.intake import service


def fixture(folder,stamp,total,translation=False):
    path=folder/(stamp+('.translation' if translation else '')+'.gkg.csv.zip');lines=[]
    for index in range(1,total+1):
        row=['']*27;row[0]=stamp+('-T' if translation else '-')+str(index);row[1]=stamp;row[2]='1';row[3]='qa.example';row[4]='https://qa.example/'+('translated' if translation else 'english')+'/'+str(index)
        row[7]='DIPLOMACY';row[11]='Alice;Bob';row[13]='QA Organization'
        if index!=17:row[26]='<PAGE_TITLE>演示材料：Alice met Bob yesterday '+str(index)+'</PAGE_TITLE>'
        lines.append('\t'.join(row))
    if not translation:lines.append('malformed retained line')
    with ZipFile(path,'w') as z:z.writestr(stamp+('.translation' if translation else '')+'.gkg.csv','\n'.join(lines)+'\n')
    raw=path.read_bytes();return path,dict(stamp=stamp,bytes=len(raw),md5=hashlib.md5(raw).hexdigest())


def main():
    import redislite
    from redis import Redis
    out=Path(sys.argv[1]).resolve();out.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='n2d-intake-ui-') as tmp:
        folder=Path(tmp);gp,hp=port(),port();binary=Path(redislite.__file__).parent/'bin'
        with (out/'falkor.log').open('w') as log:
            process=subprocess.Popen([str(binary/'redis-server'),'--loadmodule',str(binary/'falkordb.so'),'--port',str(gp),'--bind','127.0.0.1','--dir',tmp,'--save','','--appendonly','no'],stdout=log,stderr=subprocess.STDOUT)
            server=None
            try:
                client=Redis(port=gp,socket_connect_timeout=.2)
                for _ in range(100):
                    try:
                        if client.ping():break
                    except Exception:time.sleep(.05)
                fixtures={'gkg_english':fixture(folder,'20261104101500',303),'gkg_translation':fixture(folder,'20261104071500',3,True)}
                def discover(feed):return dict(fixtures[feed][1])
                def download(feed,stamp,destination,expected_md5='',expected_bytes=0,stop=None):
                    assert stamp==fixtures[feed][1]['stamp'];destination=Path(destination);destination.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(fixtures[feed][0],destination)
                service.discover=discover;service.download=download
                os.environ.update(NEWS2DOUYIN_FALKOR_URL=f'redis://127.0.0.1:{gp}',NEWS2DOUYIN_GRAPH_PREFIX='intake_qa',NEWS2DOUYIN_GKG_STREAM_ENABLED='1',NEWS2DOUYIN_GKG_PROJECTION_ENABLED='1',NEWS2DOUYIN_EVENT_READER='rules',NEWS2DOUYIN_INTAKE_TOKEN='qa-only-token-not-production')
                app=create_app(db_url='sqlite:///'+str(folder/'app.db'),storage_root=str(folder/'runs'))
                server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=hp,log_level='warning'));thread=threading.Thread(target=server.run,daemon=True);thread.start()
                for _ in range(100):
                    try:
                        if requests.get(f'http://127.0.0.1:{hp}/api/health',timeout=.3).ok:break
                    except Exception:time.sleep(.05)
                env=dict(os.environ,N2D_QA_URL=f'http://127.0.0.1:{hp}',N2D_QA_OUTPUT=str(out))
                result=subprocess.run(['node',str(ROOT/'tools/check_intake_browser.cjs')],env=env,timeout=180)
                if result.returncode:raise SystemExit(result.returncode)
                with sqlite3.connect(folder/'app.db') as db:assert db.execute('PRAGMA integrity_check').fetchall()==[('ok',)]
                print('INTAKE_REAL_DUCKDB_FALKOR_BROWSER_PASS; upstream fixtures, not public global ingestion; SQLite integrity ok')
            finally:
                if server:server.should_exit=True;thread.join(timeout=40)
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait()

if __name__=='__main__':main()
