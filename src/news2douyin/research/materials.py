"""Keep aggregator headlines and excerpts distinct from acquired original text."""
import json


LEAD_KINDS = {'event_metadata', 'newsnow_metadata', 'search_snippet'}
RECONSTRUCTION_KIND = 'gdelt_ngram_reconstruction'


def reconstruction_lead(item):
    """A frozen piece of NGrams context is always an unverified research lead."""
    return dict(provider='gdelt_web_ngrams', source_kind='search_snippet',
                material_kind=RECONSTRUCTION_KIND, content_kind='reconstructed_excerpt',
                requires_original_text=True, reconstruction=item, language=item.get('language') or '',
                content=item.get('excerpt') or '', official_event_id=item.get('event_id') or '')


def raw_metadata(article):
    try:
        value = json.loads(article.raw_json or '{}')
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError, AttributeError):
        return {}


def source_document(document, raw=None):
    """Return a copy; never change an existing Article or immutable snapshot.

    NewsNow supplies headlines/excerpts, not a fetched original article. Older
    NewsNow snapshots without the new marker are classified conservatively by
    their provider, while all other providers keep their previous semantics.
    """
    result = dict(document)
    raw = raw if isinstance(raw, dict) else result.get('raw', {})
    if not isinstance(raw, dict): raw = {}
    newsnow = result.get('newsnow', raw.get('newsnow', {}))
    if (result.get('source_kind') == 'newsnow_metadata' or result.get('provider') == 'newsnow'
            or result.get('material_kind', raw.get('material_kind')) == 'newsnow_discovery'):
        result.update(source_kind='newsnow_metadata', material_kind='newsnow_discovery',
                      content_kind=result.get('content_kind', raw.get('content_kind')) or
                          ('source_excerpt' if result.get('content') else 'title_only'),
                      requires_original_text=True,
                      newsnow=dict(newsnow) if isinstance(newsnow, dict) else {})
    elif result.get('material_kind', raw.get('material_kind')) == 'candidate_lead':
        result.update(source_kind='search_snippet', material_kind='candidate_lead',
                      content_kind=result.get('content_kind', raw.get('content_kind')) or 'title_only',
                      requires_original_text=True)
    return result


def article_document(article):
    return source_document(dict(provider=article.provider, title=article.title, content=article.content),
                           raw=raw_metadata(article))


def is_lead(document):
    return source_document(document).get('source_kind') in LEAD_KINDS


def source_label(source):
    if source.get('material_kind') == RECONSTRUCTION_KIND:return 'GDELT 片段重建，待核对出版方原文'
    if source.get('source_kind') == 'event_metadata':return '事件线索快照，尚无原文'
    if source.get('source_kind') == 'newsnow_metadata':return 'NewsNow榜单／摘要线索，尚未取得原文'
    if source.get('source_kind') == 'search_snippet':return '报道标题／摘要线索，尚未取得原文'
    return '已保存可读材料' if source.get('has_readable_text') else '只有标题，待补原文'
