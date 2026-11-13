from functools import lru_cache
import json
from pathlib import Path
from sqlmodel import select
from ..storage.models import ArticleEventLink,ArticleVersion
from ..storage.articles import lock_news
from ..storage.utils import loads
from ..events.workbench import require_event
from .models import EventPlace

@lru_cache
def countries():
    file=Path(__file__).resolve().parents[1]/'server/data/countries.json'
    return {r['a2'].lower():r['zh'] for r in json.loads(file.read_text(encoding='utf-8'))}

def add(session,event,role,iso,article,revision,quote):
    from hashlib import sha256
    import json
    lock_news(session, 'event:' + event);require_event(session,event,active=True)
    iso=iso.strip().lower();quote=quote.strip()
    if role not in ('action','impact') or iso not in countries():raise ValueError('请指定有效地理角色和国家/地区')
    if not session.exec(select(ArticleEventLink.id).where(ArticleEventLink.event_key==event,
            ArticleEventLink.article_key==article)).first():raise ValueError('请引用本事件的报道')
    v=session.exec(select(ArticleVersion).where(ArticleVersion.article_key==article,ArticleVersion.revision==revision)).first()
    doc=loads(v.payload_json,{}) if v else {}
    if not quote or not any(quote in (doc.get(field) or '') for field in ('title','content')):
        raise ValueError('摘录须来自所选报道版本的标题或正文')
    key=sha256(json.dumps([event,role,iso,article,revision,quote]).encode()).hexdigest()[:32]
    session.merge(EventPlace(key=key,event=event,role=role,iso=iso,article=article,revision=revision,quote=quote))
    session.commit()
    return {'key':key}
