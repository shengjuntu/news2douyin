"""Frozen publisher-network attribution for observed media hosts."""
from functools import lru_cache
from ipaddress import IPv6Address
import re

from ._iheart_station_hosts import IHEART_HOSTS


MAP_VERSION = '2026-11-15.v1'
NBC_LOCAL_HOSTS = frozenset({
    'nbcbayarea.com', 'nbcboston.com', 'nbcchicago.com', 'nbcconnecticut.com',
    'nbcdfw.com', 'nbclosangeles.com', 'nbcmiami.com', 'nbcnewyork.com',
    'nbcphiladelphia.com', 'nbcsandiego.com', 'nbcwashington.com',
})
_IHEART = ('family:iheart', 'iHeartMedia')
_NBC_LOCAL = ('family:nbc-local', 'NBCUniversal Local')
_UNKNOWN = ('', '')
_DNS_LABEL = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
_DNS_HOST = re.compile(rf'{_DNS_LABEL}(?:\.{_DNS_LABEL})*', re.ASCII)
_PORT = re.compile(r'[0-9]{1,5}', re.ASCII)


def publisher_unit(raw_host: str) -> tuple[str, str]:
    """Return (breadth key, known group name); unknown domains keep individual keys.

    Accept a hostname with an optional port, plus bracketed or bare IPv6.
    A URL, malformed input or missing host has no unit. Never infer independence.
    """
    if not isinstance(raw_host, str) or not raw_host or len(raw_host) > 320:
        return _UNKNOWN
    return _lookup(raw_host.strip().lower())


@lru_cache(maxsize=8192)
def _lookup(host: str) -> tuple[str, str]:
    if not host:
        return _UNKNOWN
    if host.startswith('['):
        address, close, suffix = host[1:].partition(']')
        if not close or (suffix and not (suffix.startswith(':') and _valid_port(suffix[1:]))):
            return _UNKNOWN
        return _ipv6_unit(address)
    if host.count(':') > 1:
        return _ipv6_unit(host)
    if ':' in host:
        host, port = host.rsplit(':', 1)
        if not _valid_port(port):
            return _UNKNOWN
    host = host.removesuffix('.')
    if not host.isascii():
        try:
            host = host.encode('idna').decode('ascii')
        except UnicodeError:
            return _UNKNOWN
    if len(host) > 253 or not _DNS_HOST.fullmatch(host):
        return _UNKNOWN
    host = host.removeprefix('www.')
    if host in IHEART_HOSTS:
        return _IHEART
    if host in NBC_LOCAL_HOSTS:
        return _NBC_LOCAL
    return 'host:' + host, ''


def _valid_port(port: str) -> bool:
    return bool(_PORT.fullmatch(port)) and 0 < int(port) <= 65535


def _ipv6_unit(host: str) -> tuple[str, str]:
    try:
        if '%' in host:
            return _UNKNOWN
        return 'host:' + IPv6Address(host).compressed, ''
    except ValueError:
        return _UNKNOWN
