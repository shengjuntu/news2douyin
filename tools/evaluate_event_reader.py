"""Offline headline audit. Pass the original GKG ZIP; no news text is redistributed."""
import argparse
from collections import Counter
import json
from pathlib import Path
from types import SimpleNamespace
from news2douyin.collect.providers.gkg import parse_archive
from news2douyin.extraction.reader import extract


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('archive',type=Path)
    ap.add_argument('--labels',type=Path,default=Path(__file__).resolve().parents[1]/'tests/fixtures/event-reader-headlines.json')
    opts = ap.parse_args()
    test = json.loads(opts.labels.read_text(encoding='utf-8'))
    articles = parse_archive(opts.archive.read_bytes(),test['batch'],dict(search_query=test['query'],max_items=test['limit']))
    truth = test['labels']
    seen = set()
    tp = fp = fn = exact = 0
    for article in articles:
        key = article['gkg']['record_id'].split('-')[-1]
        if key not in truth:
            continue
        seen.add(key)
        prediction,_ = extract(SimpleNamespace(article_key=key,revision=1),article,'rules')
        found = Counter(m['kind']+'/'+m['status'] for m in prediction)
        gold = Counter(truth[key])
        tp += sum((found & gold).values())
        fp += sum((found-gold).values())
        fn += sum((gold-found).values())
        exact += found==gold
        if found != gold:
            print(key,'expected',dict(gold),'found',dict(found))
    if seen != set(truth):
        raise SystemExit('Sample records missing; check the input archive.')
    print(json.dumps(dict(headlines=len(seen), exact=exact,tp=tp,fp=fp,fn=fn,
        precision=tp/(tp+fp) if tp+fp else 0,recall=tp/(tp+fn) if tp+fn else 0,
        f1=2*tp/(2*tp+fp+fn) if tp+fp+fn else 0),indent=2))


if __name__ == '__main__':
    main()
