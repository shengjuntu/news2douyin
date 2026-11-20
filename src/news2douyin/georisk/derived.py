"""Replay-safe local document identities, independent of the shared raw lake."""
from contextlib import contextmanager

from .service import MAX_EVIDENCE

SCHEMA = """
CREATE TABLE IF NOT EXISTS risk_doc (
    day VARCHAR, scope VARCHAR, category VARCHAR, doc_key VARCHAR, domain VARCHAR,
    negative DOUBLE, tone_key VARCHAR, title VARCHAR, url VARCHAR, evidence_key VARCHAR,
    PRIMARY KEY(day,scope,category,doc_key)
);
CREATE TABLE IF NOT EXISTS risk_batch (
    feed VARCHAR, stamp VARCHAR, sha VARCHAR, day VARCHAR, rejected BIGINT,
    PRIMARY KEY(feed,stamp)
);
CREATE TABLE IF NOT EXISTS risk_dirty (day VARCHAR PRIMARY KEY);
"""
INSERT_DOCS = """
INSERT INTO risk_doc
SELECT ?, doc.scope, doc.category, doc.doc_key, doc.domain, doc.negative, doc.tone_key,
       doc.title, doc.url, doc.evidence_key
FROM (SELECT unnest(?::STRUCT(scope VARCHAR, category VARCHAR, doc_key VARCHAR, domain VARCHAR,
      negative DOUBLE, tone_key VARCHAR, title VARCHAR, url VARCHAR, evidence_key VARCHAR)[]) AS doc)
ON CONFLICT(day,scope,category,doc_key) DO UPDATE SET
    negative=CASE WHEN excluded.tone_key IS NOT NULL AND
        (risk_doc.tone_key IS NULL OR excluded.tone_key<risk_doc.tone_key)
        THEN excluded.negative ELSE risk_doc.negative END,
    tone_key=least(excluded.tone_key,risk_doc.tone_key),
    title=CASE WHEN excluded.evidence_key IS NOT NULL AND
        (risk_doc.evidence_key IS NULL OR excluded.evidence_key<risk_doc.evidence_key)
        THEN excluded.title ELSE risk_doc.title END,
    url=CASE WHEN excluded.evidence_key IS NOT NULL AND
        (risk_doc.evidence_key IS NULL OR excluded.evidence_key<risk_doc.evidence_key)
        THEN excluded.url ELSE risk_doc.url END,
    evidence_key=least(excluded.evidence_key,risk_doc.evidence_key)
"""


@contextmanager
def transaction(conn):
    conn.execute('BEGIN')
    try:
        yield
        conn.execute('COMMIT')
    except BaseException:
        conn.execute('ROLLBACK')
        raise


def summarize(conn, day):
    available, rejected = conn.execute(
        'SELECT count(*),coalesce(sum(rejected),0) FROM risk_batch WHERE day=?', [day]).fetchone()
    groups = {}
    for scope, category, docs, sources, scored, negative in conn.execute(
        '''SELECT scope,category,count(*),count(DISTINCT domain),count(negative),avg(negative)
           FROM risk_doc WHERE day=? GROUP BY scope,category''', [day]
    ).fetchall():
        groups.setdefault(scope, {})[category] = dict(doc_count=docs, source_count=sources,
            tone_doc_count=scored, negative_tone=negative)
    evidence = {}
    for scope, category, title, url, domain in conn.execute(
        '''WITH by_source AS (
             SELECT scope,category,title,url,domain,negative,doc_key FROM risk_doc
             WHERE day=? AND evidence_key IS NOT NULL
             QUALIFY row_number() OVER(PARTITION BY scope,category,domain
                ORDER BY negative DESC NULLS LAST,doc_key)=1
           )
           SELECT scope,category,title,url,domain FROM by_source
           QUALIFY row_number() OVER(PARTITION BY scope,category
              ORDER BY negative DESC NULLS LAST,doc_key)<=?
           ORDER BY scope,category,negative DESC NULLS LAST,doc_key''', [day, MAX_EVIDENCE]
    ).fetchall():
        evidence.setdefault(scope, {}).setdefault(category, []).append(dict(title=title, url=url, domain=domain))
    return available, rejected, groups, evidence
