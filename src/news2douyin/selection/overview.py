"""Small receipt-table summaries, without scanning the raw lake or event graph."""
from datetime import datetime, timezone
from sqlalchemy import case, func, or_
from sqlmodel import select

from ..intake.feeds import FEEDS
from ..intake.models import FeedState, IntakeBatch


def _time(stamp):
    if not stamp:return ''
    try:return datetime.strptime(stamp,'%Y%m%d%H%M%S').replace(tzinfo=timezone.utc).isoformat().replace('+00:00','Z')
    except ValueError:return ''


def overview(session):
    states={row.key:row for row in session.exec(select(FeedState).where(FeedState.key.in_(FEEDS))
        .execution_options(n2d_query_name='selection.overview.feeds'))}
    pending=IntakeBatch.projected_offset<IntakeBatch.rows
    unresolved=or_(IntakeBatch.state!='archived',pending)
    index_backlog=(IntakeBatch.state=='archived') & pending
    stats=session.exec(select(IntakeBatch.feed,
        func.min(case((unresolved,IntakeBatch.stamp))),
        func.sum(case((index_backlog,1),else_=0)),
        func.sum(case((index_backlog,IntakeBatch.rows-IntakeBatch.projected_offset),else_=0)))
        .where(IntakeBatch.feed.in_(FEEDS))
        .group_by(IntakeBatch.feed).execution_options(n2d_query_name='selection.overview.backlog')).all()
    grouped={feed:(first,int(batches or 0),int(rows or 0)) for feed,first,batches,rows in stats}
    feeds=[]
    for key,spec in FEEDS.items():
        state=states.get(key)
        first,batches,rows=grouped.get(key,('',0,0))
        completed=select(IntakeBatch.stamp).where(IntakeBatch.feed==key,IntakeBatch.state=='archived',
            IntakeBatch.projected_offset>=IntakeBatch.rows)
        if first:completed=completed.where(IntakeBatch.stamp<first)
        indexed=session.exec(completed.order_by(IntakeBatch.stamp.desc()).limit(1)
            .execution_options(n2d_query_name='selection.overview.indexed')).first()
        feeds.append(dict(feed=key,label=spec['label'],kind=spec['kind'],
            enabled=state.enabled if state else None,
            archived_at=_time(state.last_complete) if state else '',
            indexed_at=_time(indexed),pending_batches=batches,pending_rows=rows,
            last_error='接收出现问题，请到采集进度查看。' if state and state.last_error else '',
            scope='本站所接收批次；索引时间不跨越已归档的待处理缺口'))
    return dict(status='available' if states else 'not_started',feeds=feeds,
        note='归档完成与建立索引分开显示；首次从最新批次起步，不代表已补全全部历史。')
