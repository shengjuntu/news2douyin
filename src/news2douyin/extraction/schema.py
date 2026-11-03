"""A source-grounded assertion about an action, never an assertion of world truth."""
import calendar
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

Kind = Literal['statement','meeting','visit','signing','attack','protest','decision','other']
Status = Literal['reported','planned','denied','conditional','uncertain']
Role = Literal['actor','speaker','participant','target','location','source']
LABELS = {
    'statement':'表态', 'meeting':'会谈', 'visit':'访问', 'signing':'签署', 'attack':'袭击',
    'protest':'示威', 'decision':'决定', 'other':'其他动作', 'actor':'行动者', 'speaker':'发言者',
    'participant':'参与方', 'target':'对象', 'location':'文中行动地点', 'source':'说法来源',
    'reported':'报道描述已发生', 'planned':'拟议或未来', 'denied':'否认或明确未发生',
    'conditional':'有条件', 'uncertain':'可能或待核',
}


class Argument(BaseModel):
    model_config = ConfigDict(extra='forbid')
    role: Role
    text: str = Field(min_length=1, max_length=160)
    entity_type: Literal['person','organization','place','group','unknown'] = 'unknown'
    # Optional second passage for an explicitly resolved antecedent.
    block: str = Field(default='', max_length=20)
    evidence: str = Field(default='', max_length=600)


class Candidate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    block: str = Field(max_length=20)
    quote: str = Field(min_length=1, max_length=600)
    trigger: str = Field(min_length=1, max_length=64)
    kind: Kind
    status: Status
    time: str = Field(default='', max_length=100)
    arguments: list[Argument] = Field(default_factory=list, max_length=16)


@dataclass(frozen=True)
class Block:
    id: str
    field: str
    start: int
    text: str


def passages(document):
    blocks = {}
    # Bound per-document processing; offsets are Python Unicode code points into the fixed revision.
    for field, prefix, limit in [('title','T',1200), ('content','P',12000)]:
        raw = (document.get(field) or '')[:limit]
        for piece in re.finditer(r'[^\r\n]{1,1000}', raw):
            number = len(blocks) + 1
            key = prefix + str(number)
            blocks[key] = Block(key, field, piece.start(), piece.group())
    return blocks


def stable_id(*values):
    raw = json.dumps(values, ensure_ascii=False, separators=(',',':'))
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def normalize_date(raw, published):
    """Return an ISO day or month only where precision and anchor permit."""
    raw = raw.strip().lower()
    try:
        anchor = date.fromisoformat((published or '')[:10])
    except ValueError:
        anchor = None
    full = re.fullmatch(r'(\d{4})[-年](\d{1,2})[-月](\d{1,2})日?', raw)
    short = re.fullmatch(r'(\d{1,2})月(\d{1,2})日', raw)
    try:
        if full:
            return date(*map(int, full.groups())).isoformat()
        if short and anchor:
            month, day = map(int,short.groups())
            if (month,day)==(anchor.month,anchor.day):
                return anchor.isoformat()
            return ''
        if not anchor:
            return ''
        delta = {'昨天':-1,'昨日':-1,'yesterday':-1,'今天':0,'今日':0,'today':0,
                 '明天':1,'明日':1,'tomorrow':1}
        if raw in delta:
            return (anchor + timedelta(days=delta[raw])).isoformat()
        if raw in ('下月','下个月','next month'):
            last = anchor.replace(day=calendar.monthrange(anchor.year, anchor.month)[1])
            return (last + timedelta(days=1)).strftime('%Y-%m')
    except (OverflowError, ValueError):
        pass
    return ''


def grounded(item, blocks, article, revision, published):
    block = blocks.get(item.block)
    if not block or block.text.count(item.quote) != 1 or item.trigger not in item.quote:
        raise ValueError('动作或摘录无法在指定段落中唯一定位')
    if item.time and item.time not in item.quote:
        raise ValueError('时间缺少原文依据')
    args = []
    start = block.start + block.text.index(item.quote)
    for arg in item.arguments:
        context = blocks.get(arg.block) if arg.block else block
        evidence = arg.evidence or item.quote
        if (not context or context.text.count(evidence) != 1 or arg.text not in evidence
                or (arg.block and not arg.evidence)):
            raise ValueError('角色或跨段引用缺少可定位的原文依据')
        offset = context.start + context.text.index(evidence)
        entity_type = arg.entity_type
        if arg.role=='location':
            entity_type='place'
        elif referent(arg.role,arg.text)=='person:donald-trump':
            entity_type='person'
        args.append(dict(role=arg.role, text=arg.text,entity_type=entity_type, field=context.field, start=offset,
                         end=offset+len(evidence), evidence=evidence))
    args.sort(key=lambda x:(x['role'],x['start'],x['text']))
    ident = stable_id(article, revision, block.field, start, item.quote, item.trigger, item.kind,
                      item.status, item.time, args)
    return dict(id=ident, article=article, revision=revision, field=block.field, start=start,
                end=start+len(item.quote), quote=item.quote, trigger=item.trigger, kind=item.kind,
                status=item.status, time=item.time, date=normalize_date(item.time,published), arguments=args)


# Only unambiguous curated equivalents are cross-language join keys; other spellings remain distinct.
def entity_key(text):
    import unicodedata
    name = ' '.join(unicodedata.normalize('NFKC', text).casefold().split())
    return {'唐纳德·特朗普':'donald trump','特朗普':'donald trump','川普':'donald trump',
            '华沙':'warsaw','巴黎':'paris','基辅':'kyiv'}.get(name, name)


def cluster_key(row):
    actors = sorted({entity_key(a['text']) for a in row['arguments'] if a['role'] in ('actor','speaker','participant')})
    places = sorted({entity_key(a['text']) for a in row['arguments'] if a['role']=='location'})
    if not actors or not places or len(row['date']) != 10 or row['kind']=='other':
        return ''
    roles = sorted({(a['role'],entity_key(a['text'])) for a in row['arguments'] if a['role']!='location'})
    return stable_id(row['kind'],row['status'],row['date'],roles,places)


def referent(role, text):
    key = entity_key(text)
    if role=='location' and key in ('paris','warsaw','kyiv'):
        return 'place:'+key
    if role!='location' and key=='donald trump':
        return 'person:donald-trump'
    return ''
