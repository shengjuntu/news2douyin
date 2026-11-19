"""Persistent questions belong to a watch. Investigations may revisit each point."""
from sqlmodel import select
from ..desk import service as desk
from ..desk.models import WatchReview
from ..storage.articles import lock_news
from ..storage.models import utc_now_iso
from .models import FocusPoint, PointReview
from .schema import stable_id


def add(s, watch, parent, text):
    lock_news(s, 'watch:' + watch)
    desk.get_watch(s,watch)
    parent = parent or watch
    if parent != watch:
        node = s.get(FocusPoint,parent)
        if not node or node.watch != watch:
            raise ValueError('上级问题不属于此焦点')
    label = ' '.join(text.split())
    if not 1 <= len(label) <= 400:
        raise ValueError('子问题长度须为 1–400 字')
    key = 'point_'+stable_id(watch,parent,label.casefold())
    if not s.get(FocusPoint,key):
        s.add(FocusPoint(key=key,watch=watch,parent=parent,text=label,created=utc_now_iso()))
        desk.enqueue_graph(s,'watch/'+watch)
    s.commit()
    return key


def points(s, watch, cutoff=None):
    stmt = select(FocusPoint).where(FocusPoint.watch==watch)
    if cutoff:
        stmt = stmt.where(FocusPoint.created<=cutoff)
    return s.exec(stmt.order_by(FocusPoint.created,FocusPoint.key)).all()


def request_review(s, watch, point):
    lock_news(s, 'watch:' + watch)
    w = desk.get_watch(s,watch)
    node = s.get(FocusPoint,point)
    if not node or node.watch != watch:
        raise KeyError('子问题不存在')
    ticket = desk.make_review(s,w,'','point-'+point+'-'+utc_now_iso()[:10])
    ticket.question = node.text
    s.add(ticket)
    s.merge(PointReview(ticket=ticket.key,point=point))
    desk.enqueue_graph(s,'watch/'+watch)
    s.commit()
    return ticket.key


def history(s, watch):
    return s.exec(select(PointReview,WatchReview).join(WatchReview,WatchReview.key==PointReview.ticket)
        .where(WatchReview.watch==watch).order_by(WatchReview.created.desc())).all()
