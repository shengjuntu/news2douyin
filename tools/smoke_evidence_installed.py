"""Exercise the installed wheel's source evidence and optional NGrams integration offline."""
from datetime import datetime, timedelta, timezone
from importlib import metadata
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient
from sqlmodel import Session

from news2douyin.intake.official_models import OfficialEvent, OfficialMention
from news2douyin.selection.ngram_enrichment import match_excerpt
from news2douyin.server.app import create_app


def main():
    assert metadata.version('news2douyin') == '0.40.0'
    os.environ['NEWS2DOUYIN_NGRAMS_ALLOW_DOWNLOAD'] = '0'
    now = datetime.now(timezone.utc) - timedelta(hours=2)
    stamp = now.strftime('%Y%m%d%H%M00')
    seen = now.isoformat(timespec='seconds').replace('+00:00', 'Z')
    key = '87654321'
    with TemporaryDirectory() as tmp:
        app = create_app(db_url='sqlite:///' + str(Path(tmp)/'work.db'), storage_root=tmp)
        engine = app.state.engine
        try:
            with Session(engine) as s:
                s.add(OfficialEvent(event_id=key, event_date=seen[:10], added_at=seen,
                    ingested_at=seen, first_seen=seen, last_seen=seen, actor1_name='Alpha Council',
                    actor2_name='Beta Council', event_code='040', event_root_code='04',
                    feed='events_english', stamp=stamp, row_index=3))
                s.add(OfficialMention(key='example',event_id=key,doc_identifier='https://news.example/brief',
                    domain='news.example',first_feed='mentions_english',first_stamp=stamp,first_row_index=2,
                    last_feed='mentions_english',last_stamp=stamp,last_row_index=2,updated_at=seen,
                    first_seen=seen,last_seen=seen))
                s.commit()
            client = TestClient(app)
            url='/api/gdelt/events/'+key
            evidence=client.get(url+'/evidence')
            assert evidence.status_code==200, evidence.text
            summary=evidence.json()
            assert summary['coverage']['documents']==1 and summary['coverage']['domains']==1
            assert summary['actor1']['name']=='Alpha Council' and len(summary['documents'])==1
            assert client.get(url+'/ngrams').json()['state']=='none'
            assert client.get(url+'/graph').status_code==200
            assert client.get('/research-assets/gdelt-evidence.js').status_code==200
            sentence='Alpha Council met with Beta Council on Monday.'
            quote,anchors=match_excerpt(sentence,'en','Alpha Council','Beta Council','04')
            assert quote==sentence and anchors
            client.close()
        finally:
            engine.dispose()
    print(json.dumps(dict(version='0.40.0', checked=['official_evidence','ngram_status','graph_status','action_excerpt','packaged_javascript'])))


if __name__ == '__main__':
    main()
