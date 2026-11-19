"""Text and HTTP links for standalone Markdown exports."""
from html import escape
import re
import unicodedata
from urllib.parse import quote, urlsplit

from ..selection.source_content import source_url
from .strategies import DEFAULT_STRATEGY


_MD = re.compile(r'([\\`*_{}\[\]()#!|~$])')


def _plain(value, limit=500):
    # In particular, prevent new headings and bidirectional overrides from
    # source content when an editor opens the standalone Markdown file.
    text = ''.join(' ' if unicodedata.category(c) in {'Cc', 'Cf', 'Cs'} else c
                   for c in str(value or ''))
    return ' '.join(text.split())[:limit]


def _md(value, limit=500):
    return _MD.sub(r'\\\1', escape(_plain(value, limit), quote=False))


def _url(value):
    url = source_url(value)
    if not url or '\\' in url or any(unicodedata.category(c) in {'Cc', 'Cf', 'Cs'} for c in url):
        return ''
    return url


def _md_link(url, label=None):
    # Angle-delimited Markdown destinations, with encoded angle brackets,
    # spaces and parentheses. Only call for validated HTTP(S) addresses.
    target = escape(quote(url, safe=":/?#[]@!$&'*+,;=%~-._"), quote=False)
    return f'[{_md(label or urlsplit(url).hostname, None)}](<{target}>)'

def selection_note(issue):
    """Identify a nondefault shortlist in a standalone export using its frozen metadata."""
    record = (issue.get('stats') or {}).get('selection')
    if not record or (record['strategy'] == DEFAULT_STRATEGY and record['limit'] == 100 and not record['options']):
        return ''
    custom = '（含自定义参数）' if record['options'] else ''
    return f"日报选取策略　{_md(record['strategy'])} · 版本 {_md(record['version'])} · 最多 {_md(record['limit'])} 条{custom}。"
