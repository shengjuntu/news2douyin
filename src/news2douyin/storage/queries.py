"""Small read contracts: count keys, fetch bounded pages, name expensive work.

Entity keys are supplied by the caller so joins/distinct/grouping keep their
business meaning. This layer does not cache results or estimate exact counts.
"""
from sqlalchemy import func
from sqlmodel import select


def project_keys(statement, key):
    """Unpaginated key projection; retain predicates, FROMs and distinct/grouping."""
    return statement.order_by(None).limit(None).offset(None).with_only_columns(
        key, maintain_column_froms=True)


def scalar(session, statement, *, name='db.scalar'):
    return session.execute(statement.execution_options(n2d_query_name=name)).scalar_one()


def all_rows(session, statement, *, name='db.rows'):
    return session.exec(statement.execution_options(n2d_query_name=name)).all()


def count_keys(session, statement, key, *, name='db.count_keys'):
    keys = project_keys(statement, key).subquery()
    return int(scalar(session, select(func.count()).select_from(keys), name=name))


def count_rows(session, model, *conditions, name=None):
    query = select(func.count()).select_from(model)
    if conditions:
        query = query.where(*conditions)
    table = getattr(model, '__tablename__', 'rows')
    return int(scalar(session, query, name=name or 'count.' + table))


def exists(session, statement, *, name='db.exists'):
    return bool(scalar(session, select(statement.order_by(None).exists()), name=name))


def page_rows(session, statement, *, offset=0, limit=30, maximum=500, name='db.page'):
    offset, limit = int(offset), int(limit)
    if offset < 0 or not 1 <= limit <= maximum:
        raise ValueError('Invalid page boundary')
    return all_rows(session, statement.offset(offset).limit(limit), name=name)
