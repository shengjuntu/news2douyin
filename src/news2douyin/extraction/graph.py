"""Both named graphs carry revision-grounded mentions. Role nodes are local evidence."""
from .schema import stable_id, referent, cluster_key


def project(query, article, rows, stamp):
    for m in rows:
        query('MATCH (a:Article {id:$a}) MERGE (m:EventMention {id:$id}) '
              'SET m.revision=$rev,m.field=$field,m.start=$start,m.end=$end,m.quote=$quote,'
              'm.kind=$kind,m.status=$status,m.trigger=$trigger,m.raw_time=$time,m.date=$day,'
              'm.review=$review,m.extractor=$extractor MERGE (a)-[r:DESCRIBES]->(m) SET r.stamp=$stamp',
              a=article,id=m['id'],rev=m['revision'],field=m['field'],start=m['start'],end=m['end'],
              quote=m['quote'],kind=m['kind'],status=m['status'],trigger=m['trigger'],
              time=m['time'],day=m['date'],review=m['decision'],extractor=m.get('extractor','rules'),stamp=stamp)
        signature = cluster_key(m)
        if signature:
            query('MATCH (m:EventMention {id:$m}) MERGE (g:OccurrenceCandidate {id:$sig}) '
                  'MERGE (m)-[:HAS_MATCH_KEY]->(g)', m=m['id'],sig=signature)
        for argument in m['arguments']:
            key = stable_id(m['id'],argument)
            query('MATCH (m:EventMention {id:$m}) MERGE (a:Argument {id:$id}) '
                  'SET a.role=$role,a.text=$text,a.entity_type=$entity_type,a.field=$field,a.start=$start,a.end=$end,a.quote=$evidence '
                  'MERGE (m)-[:HAS_ARGUMENT]->(a)',m=m['id'],id=key,**argument)
            ref = referent(argument['role'],argument['text'])
            if ref:
                query('MATCH (a:Argument {id:$a}) MERGE (e:Referent {id:$ref}) '
                      'MERGE (a)-[:REFERS_TO]->(e)', a=key,ref=ref)
    query('MATCH (:Article {id:$a})-[r:DESCRIBES]->() WHERE r.stamp<>$stamp DELETE r',a=article,stamp=stamp)


def clean(query):
    query('MATCH (m:EventMention) WHERE NOT (m)<-[:DESCRIBES]-() DETACH DELETE m')
    query('MATCH (a:Argument) WHERE NOT (a)<-[:HAS_ARGUMENT]-() DETACH DELETE a')
    query('MATCH (g:OccurrenceCandidate) WHERE NOT (g)<-[:HAS_MATCH_KEY]-() DELETE g')
    query('MATCH (e:Referent) WHERE NOT (e)<-[:REFERS_TO]-() DELETE e')
