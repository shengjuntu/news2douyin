"""Most recently observed distinct reports, within a configurable cap per host."""
from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from heapq import nlargest
from urllib.parse import urlsplit

from news2douyin.digest.selection import Candidate, SelectionContext, SelectionPick


def _key(url):
    return 'doc_' + sha256(url.encode('utf-8')).hexdigest()


def _priority(candidate: Candidate):
    seen = datetime.fromisoformat(candidate.observed_at.replace('Z', '+00:00'))
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return seen, candidate.key, candidate.source_url, candidate.title


class LatestDocuments:
    api_version = 1
    version = '1.0.0'
    needs_coarse = False

    def __init__(self, options):
        unknown = set(options) - {'max_per_domain'}
        if unknown:
            raise ValueError(f"Unknown latest-diverse options: {', '.join(sorted(unknown))}")
        self.max_per_domain = options.get('max_per_domain', 2)
        if type(self.max_per_domain) is not int or self.max_per_domain < 1:
            raise ValueError('max_per_domain must be a positive integer')

    def select(self, candidates, context: SelectionContext):
        # All candidates are visited once. Once a report is below its host's
        # best K, it cannot become eligible unless a newer version arrives.
        capacity = min(self.max_per_domain, context.limit)
        hosts = defaultdict(dict)
        for candidate in candidates:
            url = candidate.source_url
            try:
                parsed = urlsplit(url)
                host = parsed.hostname
                if parsed.scheme not in ('https', 'http') or not host or parsed.username or parsed.password:
                    continue
                priority = _priority(candidate)
            except (TypeError, ValueError):
                continue
            if _key(url) in context.prior_keys or candidate.key in context.prior_keys:
                continue
            documents = hosts[host]
            if url in documents:
                if priority <= documents[url][0]:
                    continue
            elif len(documents) >= capacity:
                oldest = min(documents, key=lambda u: documents[u][0])
                if priority <= documents[oldest][0]:
                    continue
                del documents[oldest]
            documents[url] = (priority, candidate)

        leaders = (record for docs in hosts.values() for record in docs.values())
        for (seen, *_), candidate in nlargest(context.limit, leaders, key=lambda r: r[0]):
            yield SelectionPick(candidate=candidate, key=_key(candidate.source_url),
                                score=seen.timestamp(), reasons=(
                                    f'按最近观测时间入选，每个来源域名最多 {self.max_per_domain} 篇',))
