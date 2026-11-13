"""Read-path audit: inspect compiled CTE trees rather than trusting WITH text."""
import re

from sqlalchemy.sql import visitors
from sqlalchemy.sql.ddl import ExecutableDDLElement
from sqlalchemy.sql.dml import UpdateBase
from sqlalchemy.sql.selectable import SelectBase, TextualSelect
from sqlalchemy.sql.elements import TextClause


def _simple_read(statement):
    # Some metadata probes use driver SQL/TextClause and have no typed SELECT AST.
    # Keep that narrow legacy allowance, but never accept an unverified raw WITH
    # or multiple statements. PRAGMA assignments are writes, unlike a query.
    statement = statement.strip().rstrip(';').strip()
    if ';' in statement:
        return False
    if re.match(r'^(SELECT|SHOW)\b', statement, re.I):
        return True
    return bool(re.fullmatch(
        r'PRAGMA\s+(?:\w+\.)?(?:database_list|compile_options|journal_mode|'
        r'foreign_keys|busy_timeout|query_only)\s*', statement, re.I))


def is_read_only_execution(statement, context):
    compiled = getattr(context, 'compiled', None)
    tree = getattr(compiled, 'statement', None)
    if tree is not None:
        # A SELECT root can carry independent or nested INSERT/UPDATE/DELETE
        # CTEs. Check every reachable node before accepting that SELECT.
        if any(isinstance(node, (UpdateBase, ExecutableDDLElement, TextualSelect))
               for node in visitors.iterate(tree)):
            return False
        if isinstance(tree, SelectBase):
            return True
        if not isinstance(tree, TextClause):
            return False
    return _simple_read(statement)
