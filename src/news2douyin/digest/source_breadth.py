"""Bounded hostname and mapped-publisher evidence for the daily radar."""
from .publisher_families import MAP_VERSION, publisher_unit


MAX_DOMAIN_SAMPLE = 64


class PublisherUnits:
    """Known groups and unmapped hosts contribute once each to breadth."""
    def __init__(self):
        self.families = {}
        self.family_hosts = {}
        self.unmatched = set()

    def add(self, key, family, host):
        if family:
            self.families[key] = family
            hosts = self.family_hosts.setdefault(key, set())
            if len(hosts) < 2:
                # The lookup already validated this as a known DNS host.
                name = host.partition(':')[0].rstrip('.')
                if not name.isascii():
                    name = name.encode('idna').decode('ascii')
                hosts.add(name.removeprefix('www.'))
        elif key and len(self.unmatched) <= MAX_DOMAIN_SAMPLE:
            # The extra distinct host proves the displayed count is a lower bound.
            self.unmatched.add(key)

    def host(self, normalized):
        # The legacy raw sample already removed one optional www label. Avoid
        # reinterpreting a second such label as a verified station alias.
        lookup = 'www.' + normalized if normalized.startswith('www.') else normalized
        self.add(*publisher_unit(lookup), normalized)

    def hosts(self, values):
        for host in values:
            self.host(host)

    def units(self):
        known = [(key, family, host) for key, family in self.families.items()
                 for host in self.family_hosts[key]]
        return tuple(known) + tuple((key, '', '') for key in self.unmatched)

    def snapshot(self):
        n = len(self.families) + len(self.unmatched)
        return dict(units=min(n, MAX_DOMAIN_SAMPLE), units_capped=n > MAX_DOMAIN_SAMPLE,
                    folded=any(len(hosts) > 1 for hosts in self.family_hosts.values()),
                    mapped_families=sorted(self.families.values()),
                    unmatched_hosts=min(len(self.unmatched), MAX_DOMAIN_SAMPLE),
                    unmatched_hosts_capped=len(self.unmatched) > MAX_DOMAIN_SAMPLE,
                    mapping_version=MAP_VERSION)


class HostSample:
    """Preserve the legacy raw sample and units beyond its truncation point.

    Source projections may carry thousands of mention domains on a single
    official event. Small observations defer publisher lookup until grouping.
    """
    def __init__(self):
        self.raw = set()
        self.overflow = None
        self._largest = ''

    def add(self, value):
        host = value.strip().casefold().removeprefix('www.')
        if not host or host in self.raw:
            return
        if len(self.raw) < MAX_DOMAIN_SAMPLE + 1:
            self.raw.add(host)
            return
        if self.overflow is None:
            self.overflow = PublisherUnits()
            self.overflow.hosts(self.raw)
            self._largest = max(self.raw)
        self.overflow.host(host)
        if host < self._largest:
            self.raw.remove(self._largest)
            self.raw.add(host)
            self._largest = max(self.raw)

    def fields(self):
        # Candidate ingestion normalizes each input hostname once more.
        hosts = tuple('www.' + h if h.startswith('www.') else h for h in sorted(self.raw))
        return dict(domains=hosts,
                    publisher_units=self.overflow.units() if self.overflow is not None else ())
