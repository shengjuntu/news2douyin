"""Normalize declared languages at source boundaries and label them for reading.

Script is not language detection. Latin-script text without a supplied language
remains unknown, and a generated Events caption cannot identify source language.
"""
import os
import re
import time
from functools import lru_cache
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


MAX_LANGUAGE_LENGTH = 40
LANGUAGE_CONFIG_ENV = 'NEWS2DOUYIN_LANGUAGE_CONFIG'
_UNKNOWN = {'', 'unknown', 'und', 'unk', 'none', 'null', 'n/a'}
# Common language codes with optional script, region, variants and extensions.
_TAG = re.compile(
    r'[a-z]{2,3}(?:-[a-z]{4})?(?:-(?:[a-z]{2}|[0-9]{3}))?'
    r'(?:-(?:[a-z0-9]{5,8}|[0-9][a-z0-9]{3}))*'
    r'(?:-[a-wy-z0-9](?:-[a-z0-9]{2,8})+)*(?:-x(?:-[a-z0-9]{1,8})+)?'
)
_PROVIDER = re.compile(r'[a-z0-9_-]{1,40}')
_EMPTY_REGISTRY = ({}, {}, {})
_CODE_ALIASES = {
    'zho':'zh', 'chi':'zh', 'eng':'en', 'fra':'fr', 'fre':'fr', 'deu':'de',
    'ger':'de', 'spa':'es', 'por':'pt', 'rus':'ru', 'ukr':'uk', 'pol':'pl',
    'ara':'ar', 'jpn':'ja', 'kor':'ko', 'ita':'it', 'tur':'tr', 'hin':'hi',
    'heb':'he', 'nld':'nl', 'dut':'nl', 'vie':'vi', 'tha':'th', 'ind':'id',
    'msa':'ms', 'may':'ms', 'fas':'fa', 'per':'fa', 'ces':'cs', 'cze':'cs',
    'ell':'el', 'gre':'el', 'ron':'ro', 'rum':'ro', 'srp':'sr', 'hrv':'hr',
    'tgl':'tl', 'ben':'bn', 'urd':'ur', 'swa':'sw',
}


NAMES = {
    'zh':'中文', 'zh-cn':'简体中文', 'zh-tw':'繁体中文',
    'zh-hans':'简体中文', 'zh-hant':'繁体中文',
    'en':'英语', 'fr':'法语', 'de':'德语', 'es':'西班牙语', 'pt':'葡萄牙语',
    'ru':'俄语', 'uk':'乌克兰语', 'pl':'波兰语', 'ar':'阿拉伯语', 'ja':'日语',
    'ko':'韩语', 'it':'意大利语', 'tr':'土耳其语', 'hi':'印地语',
    'he':'希伯来语', 'nl':'荷兰语', 'vi':'越南语', 'th':'泰语', 'id':'印度尼西亚语',
    'ms':'马来语', 'fa':'波斯语', 'cs':'捷克语', 'el':'希腊语',
    'ro':'罗马尼亚语', 'sr':'塞尔维亚语', 'hr':'克罗地亚语',
    'fil':'菲律宾语', 'tl':'他加禄语', 'bn':'孟加拉语', 'ur':'乌尔都语', 'sw':'斯瓦希里语',
}


def _tag(value):
    value = value.lower().replace('_', '-')
    if len(value) > MAX_LANGUAGE_LENGTH or not _TAG.fullmatch(value):
        return ''
    primary, separator, remainder = value.partition('-')
    if primary in _UNKNOWN:
        return ''
    return _CODE_ALIASES.get(primary, primary) + separator + remainder


def _table(value, where):
    if not isinstance(value, dict):
        raise ValueError(f'{where} 须为 TOML 表')
    return value


def _configured_tag(value, where):
    tag = _tag(value.strip()) if isinstance(value, str) else ''
    if not tag:
        raise ValueError(f'{where} 须使用有效语种标识')
    return tag


def _setting_text(value, where):
    if not isinstance(value, str) or not value.strip() or len(value) > 100 or not value.isprintable():
        raise ValueError(f'{where} 须为 1–100 字的文本')
    return value.strip()


@lru_cache(maxsize=8)
def _read_registry(path, modified, size):
    with Path(path).open('rb') as stream:
        data = tomllib.load(stream)
    if set(data) - {'labels', 'providers'}:
        raise ValueError('语种配置仅支持 labels 和 providers')
    labels = {
        _configured_tag(tag, 'labels 的键'): _setting_text(label, '语种名称')
        for tag, label in _table(data.get('labels', {}), 'labels').items()
    }
    aliases, queries = {}, {}
    for name, options in _table(data.get('providers', {}), 'providers').items():
        if not _PROVIDER.fullmatch(name):
            raise ValueError('语种配置中的信源编号无效')
        options = _table(options, name)
        if set(options) - {'aliases', 'query'}:
            raise ValueError(f'{name} 的语种配置仅支持 aliases 和 query')
        local_aliases = _table(options.get('aliases', {}), f'{name}.aliases')
        incoming = {
            _setting_text(raw, '来源语种').casefold(): _configured_tag(tag, f'{name}.aliases 的值')
            for raw, tag in local_aliases.items()
        }
        if any(incoming.get(tag, tag) != tag for tag in incoming.values()):
            raise ValueError(f'{name}.aliases 的值须为最终语种标识，不能再次映射')
        aliases[name] = incoming
        local_query = _table(options.get('query', {}), f'{name}.query')
        queries[name] = {
            _configured_tag(tag, f'{name}.query 的键'): _setting_text(code, '上游语种代码')
            for tag, code in local_query.items()
        }
    return labels, aliases, queries


@lru_cache(maxsize=8)
def _check_registry(path, second):
    stat = Path(path).stat()
    return _read_registry(path, stat.st_mtime_ns, stat.st_size)


def _registry():
    path = os.getenv(LANGUAGE_CONFIG_ENV, '').strip()
    if not path:
        return _EMPTY_REGISTRY
    try:
        return _check_registry(path, int(time.monotonic()))
    except (OSError, ValueError) as exc:
        raise ValueError(f'{LANGUAGE_CONFIG_ENV} 无法加载语种配置：{exc}') from exc


def normalize_language(value, *, provider='', strict=False):
    """Return a canonical tag or an empty string for unknown language."""
    raw = value.strip() if isinstance(value, str) else ''
    if raw.casefold() in _UNKNOWN:
        return ''
    configured = _registry()[1].get(provider, {}).get(raw.casefold()) if provider else ''
    tag = configured or _tag(raw)
    if strict and not tag:
        raise ValueError('请输入有效语种代码，如 en、pt-BR 或 zh-Hant')
    return tag


def language_metadata(value, *, provider=''):
    return {
        'language': normalize_language(value, provider=provider),
        'source_language': value if isinstance(value, str) else '',
    }


def provider_languages(provider, defaults):
    """Get the exact declared query mappings without accepting unsupported tags."""
    return {**defaults, **_registry()[2].get(provider, {})}


def provider_language_code(value, *, provider='', defaults=None):
    tag = normalize_language(value, provider=provider)
    if not tag:
        return ''
    configured = _registry()[2].get(provider, {}).get(tag)
    if configured:
        return configured
    return tag if defaults is None else defaults.get(tag, '')


def describe_language(text='', declared='', *, provider=''):
    value = normalize_language(declared, provider=provider)
    if value:
        labels = _registry()[0]
        label = labels.get(value) or NAMES.get(value)
        parts = value.split('-')
        if not label and len(parts) > 2:
            prefix = '-'.join(parts[:2])
            label = labels.get(prefix) or NAMES.get(prefix)
        if not label:
            primary = parts[0]
            label = labels.get(primary) or NAMES.get(primary) or value.upper()
        return dict(code=value,label=label,basis='declared',display=label+' · 来源字段')
    sample = text[:2000] if isinstance(text,str) else ''
    if re.search(r'[\u3040-\u30ff]',sample):
        return dict(code='ja',label='可能日语',basis='script',display='可能日语 · 按文字推测')
    if re.search(r'[\uac00-\ud7af]',sample):
        return dict(code='ko',label='可能韩语',basis='script',display='可能韩语 · 按文字推测')
    # Han alone can be Chinese, Japanese or mixed. Do not mark a generated
    # Chinese category label + foreign actors as a Chinese source article.
    if re.search(r'[\u4e00-\u9fff]',sample) and not re.search(r'[A-Za-z\u0400-\u04ff\u0600-\u06ff]',sample):
        return dict(code='zh',label='可能中文',basis='script',display='可能中文 · 按文字推测')
    return dict(code='unknown',label='语言未知',basis='unknown',display='语言未知 · 无可靠语言字段')


def event_language():
    return dict(code='unknown',label='报道语言未知',basis='not_provided',
                display='报道语言未知 · Events未提供语言字段')
