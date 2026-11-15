"""Real local HTTP and PostgreSQL diagnostics without calling a public source."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

import pytest
import requests
from sqlalchemy import event as sql_event
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, select

from news2douyin.intake import service
from news2douyin.intake.fetch_diagnostics import IntakeFetchDiagnostic, save, snapshot
from news2douyin.intake.feeds import FEEDS
from news2douyin.intake.models import FeedState, IntakeBatch
from news2douyin.storage.db import init_db, make_engine
from test_postgres_integration import postgres_url
from test_official_events import event_line, zipped
from sql_readonly_audit import is_read_only_execution

STAMP='20261114041500'
NEXT='20261114043000'
FEED='events_english'


@pytest.fixture(params=['sqlite','postgres'])
def diagnostic_engine(request,tmp_path):
    url=request.getfixturevalue('postgres_url') if request.param=='postgres' else 'sqlite:///'+str(tmp_path/'diagnostics.db')
    engine=make_engine(url);init_db(engine);service.initialize(engine)
    try:yield engine,tmp_path/'runs'
    finally:engine.dispose()


@contextmanager
def upstream(monkeypatch,engine,**changes):
    body=zipped([event_line(added=STAMP)],feed=FEED,stamp=STAMP)
    config=dict(manifest_status=200,archive_status=200,latest=STAMP,blob=body,
        md5=None,missing_feed=False,calls=[],old_status=404)
    config.update(changes)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_GET(self):
            config['calls'].append(self.path)
            manifest=self.path.endswith('lastupdate.txt')
            if manifest:
                code=config['manifest_status']
                suffix='.gkg.csv.zip' if config['missing_feed'] else FEEDS[FEED]['suffix']
                url='http://data.gdeltproject.org/gdeltv2/'+config['latest']+suffix
                data=(str(len(config['blob']))+' '+(config['md5'] or hashlib.md5(config['blob']).hexdigest())+' '+url+'\n').encode()
            else:
                code=config['archive_status'] if self.path.endswith(config['latest']+FEEDS[FEED]['suffix']) else config['old_status']
                data=config['blob'] if code==200 else b'not available'
            self.send_response(code);self.send_header('Content-Length',str(len(data)))
            if code==429:self.send_header('Retry-After','120')
            if 300<=code<400:self.send_header('Location','https://untrusted.example/credentials')
            self.end_headers()
            try:self.wfile.write(data)
            except (BrokenPipeError,ConnectionResetError):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    original_get=requests.get
    def route(url,**kwargs):
        # Product URLs remain official and HTTPS; only the test transport maps
        # them to our disposable loopback server. No external request escapes.
        assert urlsplit(url).hostname=='data.gdeltproject.org'
        assert kwargs['allow_redirects'] is False and kwargs['stream'] is True
        assert engine.pool.checkedout()==0,'SQL connection held during HTTP'
        return original_get('http://127.0.0.1:'+str(server.server_port)+urlsplit(url).path,**kwargs)
    monkeypatch.setattr(service.requests,'get',route)
    try:yield config
    finally:server.shutdown();server.server_close();thread.join(timeout=2)


def diagnostic(engine):
    data=service.status(engine,None)
    return next(row['fetch_diagnostics'] for row in data['feeds'] if row['key']==FEED)


def saved_state(engine):
    with Session(engine) as s:
        row=s.get(FeedState,FEED)
        return row.model_dump()


def test_success_records_request_provenance_and_survives_model_restart(diagnostic_engine,monkeypatch):
    engine,root=diagnostic_engine
    with upstream(monkeypatch,engine) as source:
        assert service.step(engine,root,FEED,now=1000)
        item=diagnostic(engine)
        assert item['status']=='succeeded' and item['phase']=='archive'
        assert item['request_url']==service.url_for(FEED,STAMP)
        assert item['manifest_listed_url'].startswith('http://data.gdeltproject.org/')
        assert item['http_status']==200 and item['manifest_http_status']==200
        assert item['expected_md5']==hashlib.md5(source['blob']).hexdigest()
        assert item['expected_bytes']==len(source['blob'])
        assert item['attempted_at'] and item['completed_at'] and item['attempt_id']
        assert item['latest_stamp']==item['target_stamp']==STAMP
        assert item['url']==item['request_url'] and item['latest_manifest_stamp']==STAMP
        assert saved_state(engine)['last_complete']==STAMP
    init_db(engine)
    assert diagnostic(engine)==item


def test_manifest_404_is_not_misreported_as_a_missing_archive(diagnostic_engine,monkeypatch):
    engine,root=diagnostic_engine
    with upstream(monkeypatch,engine,manifest_status=404):
        assert service.step(engine,root,FEED,now=1000)
    item=diagnostic(engine)
    assert item['phase']=='manifest' and item['http_status']==404
    assert item['error_kind']=='manifest_not_found' and item['target_stamp']==''
    assert saved_state(engine)['last_complete']==saved_state(engine)['next_batch']==''
    with Session(engine) as s:assert not s.exec(select(IntakeBatch)).all()


def test_old_target_404_keeps_gap_and_exposes_distinct_latest_manifest(diagnostic_engine,monkeypatch):
    engine,root=diagnostic_engine
    with Session(engine) as s:
        row=s.get(FeedState,FEED);row.next_batch=STAMP;s.add(row);s.commit()
    with upstream(monkeypatch,engine,latest=NEXT):
        assert service.step(engine,root,FEED,now=1000)
    item=diagnostic(engine)
    assert item['phase']=='download' and item['http_status']==404
    assert item['target_stamp']==STAMP and item['latest_stamp']==NEXT
    assert item['manifest_listed_url'].endswith(NEXT+FEEDS[FEED]['suffix'])
    assert item['error_kind']=='batch_not_found_unverified'
    assert item['expected_md5']=='' and item['expected_bytes']==0
    assert saved_state(engine)['next_batch']==STAMP and not saved_state(engine)['last_complete']


@pytest.mark.parametrize('error,kind',[(requests.exceptions.SSLError,'tls_error'),(requests.exceptions.ReadTimeout,'timeout')])
def test_tls_timeout_diagnostics_never_echo_exception_credentials(diagnostic_engine,monkeypatch,error,kind):
    engine,root=diagnostic_engine
    def failed(*args,**kwargs):raise error('https://username:private-password@untrusted.example/path?token=secret')
    monkeypatch.setattr(service.requests,'get',failed)
    assert service.step(engine,root,FEED,now=1000)
    item=diagnostic(engine)
    assert item['error_kind']==kind and item['phase']=='manifest' and item['http_status'] is None
    public=json.dumps(service.status(engine,None))
    assert not any(value in public for value in ('private-password','token=secret','username:'))


def test_missing_feed_in_200_manifest_is_distinct_and_does_not_guess_a_path(diagnostic_engine,monkeypatch):
    engine,root=diagnostic_engine
    with upstream(monkeypatch,engine,missing_feed=True) as source:
        assert service.step(engine,root,FEED,now=1000)
        assert len(source['calls'])==1
    item=diagnostic(engine)
    assert item['http_status']==200 and item['error_kind']=='manifest_missing_feed'
    assert not saved_state(engine)['next_batch']


def test_rate_limit_keeps_retry_after_and_cursor(diagnostic_engine,monkeypatch):
    engine,root=diagnostic_engine
    with upstream(monkeypatch,engine,archive_status=429):
        assert service.step(engine,root,FEED,now=1000)
    item=diagnostic(engine)
    assert item['phase']=='download' and item['error_kind']=='rate_limited' and item['http_status']==429
    assert saved_state(engine)['retry_after']==1120 and saved_state(engine)['next_batch']==STAMP


def test_bad_checksum_is_not_a_network_outage_or_success(diagnostic_engine,monkeypatch):
    engine,root=diagnostic_engine
    with upstream(monkeypatch,engine,md5='0'*32):
        assert service.step(engine,root,FEED,now=1000)
    item=diagnostic(engine)
    assert item['http_status']==200 and item['error_kind']=='archive_checksum'
    assert not saved_state(engine)['last_complete']
    assert not list(root.rglob('*.part'))


def test_invalid_zip_preserves_failed_input_and_archive_stage(diagnostic_engine,monkeypatch):
    engine,root=diagnostic_engine
    with upstream(monkeypatch,engine,blob=b'not a ZIP file'):
        assert service.step(engine,root,FEED,now=1000)
    item=diagnostic(engine)
    assert item['phase']=='archive' and item['http_status']==200 and item['error_kind']=='archive_invalid'
    assert not saved_state(engine)['last_complete'] and list(root.rglob('*.rejected-*'))


def test_explicit_probe_deduplicates_same_target_and_never_writes_or_opens_lake(diagnostic_engine,monkeypatch):
    engine,root=diagnostic_engine
    before=saved_state(engine);sql=[]
    def audit(_conn,_cursor,statement,_params,context,_many):sql.append((statement,is_read_only_execution(statement,context)))
    sql_event.listen(engine,'before_cursor_execute',audit)
    monkeypatch.setattr(service,'Lake',lambda *_:(_ for _ in ()).throw(AssertionError('probe opened lake')))
    try:
        with upstream(monkeypatch,engine) as source:
            result=service.probe(engine,FEED)
            assert len(source['calls'])==2
    finally:sql_event.remove(engine,'before_cursor_execute',audit)
    assert result['manifest']['status']==result['target']['status']=='succeeded'
    assert result['latest']['same_as_target'] and not result['target']['archive_verified']
    assert result['cursor_unchanged'] and result['transport_policy']=='https_only'
    assert sql and all(read for _,read in sql)
    assert saved_state(engine)==before and not root.exists()
    assert diagnostic(engine)=={}


def test_probe_old_404_latest_200_does_not_rewrite_persistent_attempt(diagnostic_engine,monkeypatch):
    engine,root=diagnostic_engine
    with Session(engine) as s:
        row=s.get(FeedState,FEED);row.next_batch=STAMP;s.add(row);s.commit()
    with upstream(monkeypatch,engine,latest=NEXT) as source:
        assert service.step(engine,root,FEED,now=1000)
        before=saved_state(engine);previous=diagnostic(engine)
        source['calls'].clear()
        result=service.probe(engine,FEED)
        assert len(source['calls'])==3
    assert result['target']['http_status']==404 and result['latest']['http_status']==200
    assert result['manifest']['status']=='succeeded' and result['target']['error_kind']=='batch_not_found_unverified'
    assert diagnostic(engine)==previous and saved_state(engine)==before


def test_probe_does_not_follow_redirect_or_accept_arbitrary_feed_url(diagnostic_engine,monkeypatch):
    engine,_=diagnostic_engine
    with upstream(monkeypatch,engine,manifest_status=302) as source:
        result=service.probe(engine,FEED)
        assert len(source['calls'])==1
    assert result['manifest']['error_kind']=='http_redirect'
    with pytest.raises(ValueError):service.probe(engine,'https://untrusted.example/steal')


def test_status_is_sql_only_and_late_old_attempt_cannot_replace_new_owner(diagnostic_engine,monkeypatch):
    engine,root=diagnostic_engine
    with Session(engine) as s:
        row=s.get(FeedState,FEED);row.lease='owner-new';s.add(row)
        assert save(s,FEED,snapshot(FEED,attempt_id='owner-new',phase='manifest'))
        s.commit()
    with Session(engine) as s:
        assert save(s,FEED,snapshot(FEED,attempt_id='owner-old',status='failed')) is False
        s.commit()
    def forbidden(*args,**kwargs):raise AssertionError('status attempted network/filesystem lake access')
    monkeypatch.setattr(service.requests,'get',forbidden);monkeypatch.setattr(service,'Lake',forbidden)
    result=service.status(engine,root)
    assert result['lake_exists'] is None and result['lake_state']=='not_probed_in_status'
    assert diagnostic(engine)['attempt_id']=='owner-new'


def test_recovered_lease_does_not_leave_old_running_diagnostics_looking_active(diagnostic_engine):
    engine,_=diagnostic_engine
    with Session(engine) as s:
        row=s.get(FeedState,FEED);row.lease='interrupted-process';row.lease_until=9999999999;s.add(row)
        assert save(s,FEED,snapshot(FEED,attempt_id=row.lease,phase='download',target_stamp=STAMP))
        s.commit()
    assert service.recover_leases(engine)
    item=diagnostic(engine)
    assert item['status']=='interrupted' and item['phase']=='download'
    assert item['target_stamp']==STAMP and not saved_state(engine)['last_complete']


def test_probe_command_uses_readonly_database_without_runtime_initialization(diagnostic_engine,monkeypatch,capsys):
    from news2douyin.intake.fetch_diagnostics import main
    engine,_=diagnostic_engine
    before=saved_state(engine)
    def inspect_readonly(candidate,feed):
        assert feed==FEED
        with candidate.connect() as connection:
            assert connection.execute(text('SELECT key FROM feedstate LIMIT 1')).first()
            with pytest.raises(DBAPIError):connection.execute(text("UPDATE feedstate SET next_batch='must-not-write'"))
            connection.rollback()
        return dict(manifest=dict(status='succeeded'),target=dict(status='succeeded'))
    monkeypatch.setattr(service,'probe',inspect_readonly)
    capsys.readouterr()
    assert main(['--feed',FEED,'--db-url',engine.url.render_as_string(hide_password=False)])==0
    assert json.loads(capsys.readouterr().out)['target']['status']=='succeeded'
    assert saved_state(engine)==before


def test_probe_command_missing_sqlite_does_not_create_a_database(tmp_path,capsys):
    from news2douyin.intake.fetch_diagnostics import main
    path=tmp_path/'missing-directory'/'database.db'
    assert main(['--feed',FEED,'--db-url','sqlite:///'+str(path)])==2
    value=json.loads(capsys.readouterr().out)
    assert value['error_kind']=='diagnostic_unavailable' and not path.parent.exists()


def test_every_diagnostic_publication_holds_the_feed_lock_with_batch_locks(diagnostic_engine,monkeypatch):
    engine,root=diagnostic_engine
    original_lock,original_save=service.lock_news,service.fetch_diag.save
    scopes=[]
    def locked(session,*resources):
        scopes.append(resources)
        session.info.setdefault('held_feed_scopes',set()).update(resources)
        return original_lock(session,*resources)
    def guarded_save(session,feed,data):
        assert 'intake:feed:'+feed in session.info.get('held_feed_scopes',set())
        return original_save(session,feed,data)
    monkeypatch.setattr(service,'lock_news',locked)
    monkeypatch.setattr(service.fetch_diag,'save',guarded_save)
    with upstream(monkeypatch,engine):assert service.step(engine,root,FEED,now=1000)
    assert scopes[0]==('intake:feed:'+FEED,)
    pair=('intake:feed:'+FEED,'intake:batch:'+FEED+'_'+STAMP)
    assert scopes[1:]==[pair,pair,pair]


@pytest.mark.parametrize('lost_after',['manifest','download'])
def test_stale_receiver_stops_before_any_intermediate_batch_or_diagnostic_write(diagnostic_engine,monkeypatch,lost_after):
    engine,root=diagnostic_engine
    def replace_owner():
        with Session(engine) as s:
            service.lock_news(s,'intake:feed:'+FEED)
            row=s.get(FeedState,FEED);row.lease='new-owner';s.add(row)
            assert save(s,FEED,snapshot(FEED,attempt_id='new-owner',phase='manifest',message='new owner evidence'))
            s.commit()
    with upstream(monkeypatch,engine):
        name='discover' if lost_after=='manifest' else 'download'
        original=getattr(service,name)
        def lose_lease(*args,**kwargs):
            result=original(*args,**kwargs);replace_owner();return result
        monkeypatch.setattr(service,name,lose_lease)
        assert service.step(engine,root,FEED,now=1000)
    item=diagnostic(engine)
    assert item['attempt_id']=='new-owner' and item['message']=='new owner evidence'
    assert not saved_state(engine)['last_complete']
    with Session(engine) as s:
        batch=s.get(IntakeBatch,FEED+'_'+STAMP)
        if lost_after=='manifest':assert batch is None
        else:assert batch and not batch.sha256 and not batch.archive_path and batch.state=='pending'
