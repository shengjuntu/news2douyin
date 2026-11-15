"""Conservative language labels from declared metadata or visible script.

Script is not language detection. Latin-script text without a supplied language
remains unknown, and a generated Events caption cannot identify source language.
"""
import re


NAMES = {
    'zh':'中文', 'zh-cn':'简体中文', 'zh-tw':'繁体中文', 'en':'英语', 'eng':'英语',
    'fr':'法语', 'fra':'法语', 'fre':'法语', 'de':'德语', 'deu':'德语', 'ger':'德语',
    'es':'西班牙语', 'spa':'西班牙语', 'pt':'葡萄牙语', 'por':'葡萄牙语',
    'ru':'俄语', 'rus':'俄语', 'uk':'乌克兰语', 'ukr':'乌克兰语', 'pl':'波兰语', 'pol':'波兰语',
    'ar':'阿拉伯语', 'ara':'阿拉伯语', 'ja':'日语', 'jpn':'日语', 'ko':'韩语', 'kor':'韩语',
    'it':'意大利语', 'ita':'意大利语', 'tr':'土耳其语', 'tur':'土耳其语',
    'hi':'印地语', 'hin':'印地语', 'he':'希伯来语', 'heb':'希伯来语', 'nl':'荷兰语', 'nld':'荷兰语',
    'vi':'越南语', 'vie':'越南语', 'th':'泰语', 'tha':'泰语', 'id':'印度尼西亚语', 'ind':'印度尼西亚语',
}


def describe_language(text='', declared=''):
    value = declared.strip().lower().replace('_','-') if isinstance(declared,str) else ''
    if value and value not in {'unknown','und','unk','none','null'} and re.fullmatch(r'[a-z]{2,3}(?:-[a-z0-9]{2,8})*',value):
        label = NAMES.get(value,NAMES.get(value.split('-')[0],value.upper()))
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
