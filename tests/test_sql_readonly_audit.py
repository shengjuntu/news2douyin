from types import SimpleNamespace

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, column, delete, insert, literal, select, table, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from sql_readonly_audit import is_read_only_execution


def audit(statement):
    compiled = statement.compile(dialect=postgresql.dialect())
    return is_read_only_execution(str(compiled), SimpleNamespace(compiled=compiled))


def test_compiled_read_only_cte_and_nested_cte_are_allowed():
    source = table('items', column('id'))
    first = select(source.c.id).cte('first')
    second = select(first.c.id).cte('second')
    assert audit(select(second.c.id))
    assert audit(select(literal('DELETE is only a bound value')))


@pytest.mark.parametrize('kind', ['delete', 'update', 'insert'])
def test_select_with_a_writing_cte_is_rejected_without_executing(kind):
    source = table('items', column('id'))
    mutation = {'delete': delete(source).where(source.c.id == 1),
                'update': update(source).values(id=2),
                'insert': insert(source).values(id=3)}[kind]
    changed = mutation.returning(source.c.id).cte('changed')
    # Unreferenced add_cte still executes in PostgreSQL; inspecting the SELECT
    # output columns or first word alone would miss its side effect.
    statement = select(literal(1)).add_cte(changed)
    assert str(statement).startswith('WITH changed AS')
    assert not audit(statement)


@pytest.mark.parametrize('statement', [
    'WITH changed AS (DELETE FROM items RETURNING id) SELECT id FROM changed',
    'WITH data AS (SELECT 1) SELECT * FROM data',
    'SELECT 1; DELETE FROM items',
    'PRAGMA journal_mode=WAL',
])
def test_unverified_raw_ctes_and_writes_are_rejected(statement):
    assert not audit(text(statement))


def test_metadata_queries_still_work_without_compiled_ast():
    context = SimpleNamespace(compiled=None)
    assert is_read_only_execution('SHOW standard_conforming_strings', context)
    assert is_read_only_execution('PRAGMA journal_mode', context)


def test_ddl_and_opaque_textual_cte_cannot_hide_behind_a_select():
    assert not audit(CreateTable(Table('items', MetaData(), Column('id', Integer))))
    opaque = text('DELETE FROM items RETURNING id').columns(id=Integer).cte('changed')
    assert not audit(select(opaque.c.id))
