"""Sample intake progress without opening production databases or raw archives.

Example: python tools/sample_intake_status.py --base-url http://127.0.0.1:3180
--seconds 900 --interval 30 --output intake-sample.json
Rates describe this observed window, not original-news volume or model quality.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
from urllib.parse import urlsplit
import requests


def value(mapping, *names):
    for name in names:
        number=mapping.get(name)
        if isinstance(number,(int,float)) and not isinstance(number,bool) and math.isfinite(number):return number
    return None


def difference(first, last, elapsed):
    if first is None or last is None or elapsed<=0:return dict(delta=None,per_second=None,state='unknown')
    if last<first:return dict(delta=None,per_second=None,state='counter_reset_or_scope_change')
    return dict(delta=last-first,per_second=round((last-first)/elapsed,6),state='observed')


def summarize(samples):
    valid=[s for s in samples if isinstance(s.get('data'),dict) and s['data'].get('database_ok') is not False]
    if len(valid)<2:return dict(state='insufficient',note='至少需要两次成功观察；失败不是零进度。')
    first,last=valid[0],valid[-1];elapsed=last['elapsed_seconds']-first['elapsed_seconds']
    a,b=first['data'],last['data'];pa,pb=a.get('projection',{}),b.get('projection',{})
    ca,cb=a.get('counters',{}),b.get('counters',{})
    per_feed=[]
    def feeds(payload):
        source=payload.get('per_feed') or []
        if isinstance(source,dict):return source
        return {row.get('feed') or row.get('key'):row for row in source if isinstance(row,dict)}
    fa,fb=feeds(pa),feeds(pb)
    for key in sorted(set(fa)|set(fb)):
        start,end=fa.get(key,{}),fb.get(key,{})
        per_feed.append(dict(feed=key,processed=difference(value(start,'processed_rows'),value(end,'processed_rows'),elapsed),
            scope=end.get('counts_scope') or end.get('scope') or 'see_source_snapshot'))
    return dict(state='observed',elapsed_seconds=round(elapsed,3),successful_samples=len(valid),
        failed_samples=len(samples)-len(valid),
        archived=difference(value(ca,'rows'),value(cb,'rows'),elapsed),
        processed=difference(value(pa,'processed_rows'),value(pb,'processed_rows'),elapsed),
        waiting_first=value(pa,'waiting') if value(pa,'waiting') is not None else value(ca,'backlog_rows'),
        waiting_last=value(pb,'waiting') if value(pb,'waiting') is not None else value(cb,'backlog_rows'),
        projected_scope_first=pa.get('counts_scope'),projected_scope_last=pb.get('counts_scope'),per_feed=per_feed,
        note='processed是源行游标推进量，包含显式skipped，不是新增文章数；不同流分开比较。原始响应保留在samples中。')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',default='http://127.0.0.1:3180')
    parser.add_argument('--seconds',type=float,default=900)
    parser.add_argument('--interval',type=float,default=30)
    parser.add_argument('--output',type=Path,default=Path('intake-sample.json'))
    args=parser.parse_args();url=urlsplit(args.base_url)
    if url.scheme not in ('http','https') or not url.hostname or url.username or url.password or url.query or url.fragment:
        parser.error('base-url须为无账号、查询参数的HTTP/HTTPS服务地址')
    if not math.isfinite(args.seconds) or not math.isfinite(args.interval) or args.seconds<0 or args.interval<1:
        parser.error('seconds须>=0，interval须>=1秒')
    started=time.monotonic();samples=[]
    try:
        while True:
            row=dict(at=datetime.now(timezone.utc).isoformat(timespec='seconds'),elapsed_seconds=round(time.monotonic()-started,3))
            try:
                response=requests.get(args.base_url.rstrip('/')+'/api/intake/status',timeout=(5,15))
                row['http_status']=response.status_code;response.raise_for_status()
                data=response.json()
                if not isinstance(data,dict):raise ValueError('status_response_is_not_an_object')
                row['data']=data
            except (requests.RequestException,ValueError) as exc:
                row['error']=type(exc).__name__
            samples.append(row)
            output=dict(schema_version=1,samples=samples,summary=summarize(samples))
            args.output.parent.mkdir(parents=True,exist_ok=True)
            temporary=args.output.with_name(args.output.name+'.part')
            temporary.write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n');temporary.replace(args.output)
            print(json.dumps(dict(sample=len(samples),elapsed=row['elapsed_seconds'],status=row.get('http_status'),error=row.get('error'))),flush=True)
            remaining=args.seconds-(time.monotonic()-started)
            if remaining<=0:break
            time.sleep(min(args.interval,remaining))
    except KeyboardInterrupt:pass
    print(str(args.output.resolve()))


if __name__=='__main__':main()
