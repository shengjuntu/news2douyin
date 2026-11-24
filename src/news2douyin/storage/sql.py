"""Small SQL expressions shared by SQLite compatibility and PostgreSQL runtime.

Application JSON remains immutable text on both backends. Safe readers keep an
invalid legacy payload from aborting a transaction; they never rewrite it.
"""
import re
from sqlalchemy import Boolean, Float, String, literal
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement


class _JsonText(FunctionElement):
    type=String()
    inherit_cache=True


class _JsonString(FunctionElement):
    type=Boolean()
    inherit_cache=True


class _TimeValue(FunctionElement):
    type=Float()
    inherit_cache=True


def json_text(column,key):
    if not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*',key):raise ValueError('Unsupported JSON field')
    return _JsonText(column,literal(key))


def json_is_string(column,key):
    if not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*',key):raise ValueError('Unsupported JSON field')
    return _JsonString(column,literal(key))


def time_value(column):
    """Comparable timestamp scalar, NULL for empty or invalid legacy dates."""
    return _TimeValue(column)


@compiles(_JsonText,'sqlite')
def _sqlite_json_text(element,compiler,**kw):
    column,key=[compiler.process(c,**kw) for c in element.clauses]
    return f"json_extract(CASE WHEN json_valid({column}) THEN {column} ELSE '{{}}' END, '$.' || {key})"


@compiles(_JsonString,'sqlite')
def _sqlite_json_string(element,compiler,**kw):
    column,key=[compiler.process(c,**kw) for c in element.clauses]
    return f"(json_type(CASE WHEN json_valid({column}) THEN {column} ELSE '{{}}' END, '$.' || {key}) = 'text')"


@compiles(_JsonText,'postgresql')
def _pg_json_text(element,compiler,**kw):
    column,key=[compiler.process(c,**kw) for c in element.clauses]
    return f"(n2d_jsonb_or_null({column}) ->> {key})"


@compiles(_JsonString,'postgresql')
def _pg_json_string(element,compiler,**kw):
    column,key=[compiler.process(c,**kw) for c in element.clauses]
    return f"(jsonb_typeof(n2d_jsonb_or_null({column}) -> {key}) = 'string')"


@compiles(_TimeValue,'sqlite')
def _sqlite_time(element,compiler,**kw):
    return 'julianday('+compiler.process(next(iter(element.clauses)),**kw)+')'


@compiles(_TimeValue,'postgresql')
def _pg_time(element,compiler,**kw):
    value=compiler.process(next(iter(element.clauses)),**kw)
    return f'CAST(EXTRACT(EPOCH FROM n2d_timestamp_or_null({value})) AS DOUBLE PRECISION)'


def install_sql_functions(connection):
    if connection.dialect.name!='postgresql':return
    # The exception handlers open subtransactions, which PostgreSQL forbids in
    # parallel queries even when the input parses successfully.
    connection.exec_driver_sql("""CREATE OR REPLACE FUNCTION n2d_jsonb_or_null(raw text)
        RETURNS jsonb LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL UNSAFE AS $n2d$
        BEGIN RETURN raw::jsonb;
        EXCEPTION WHEN data_exception THEN RETURN NULL;
        END $n2d$""")
    # Explicit UTC for timestamps without an offset matches SQLite's historical
    # behavior and avoids depending on a deployment's PostgreSQL TimeZone.
    connection.exec_driver_sql("""CREATE OR REPLACE FUNCTION n2d_timestamp_or_null(raw text)
        RETURNS timestamptz LANGUAGE plpgsql STABLE STRICT PARALLEL UNSAFE AS $n2d$
        BEGIN
          IF raw !~ '^\\d{4}-\\d{2}-\\d{2}([T ]\\d{2}:\\d{2}(:\\d{2}(\\.\\d+)?)?([Zz]|[+-]\\d{2}:\\d{2})?)?$' THEN RETURN NULL; END IF;
          IF raw ~ '([Zz]|[+-]\\d{2}:\\d{2})$' THEN RETURN raw::timestamptz; END IF;
          RETURN raw::timestamp AT TIME ZONE 'UTC';
        EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN RETURN NULL;
        END $n2d$""")
