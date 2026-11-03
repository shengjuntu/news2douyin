"""Explainable topic routing. Place mentions never supply publisher location."""
import json
import re
import unicodedata
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Literal

ALIASES = {
    '特朗普': ['donald trump','trump','川普'], '俄乌战争': ['russia ukraine war','russia-ukraine','war in ukraine','ukraine war','invasion of ukraine','俄乌冲突','russian invasion'],
    '乌克兰':['ukraine','ukrainian','kyiv','基辅'], '俄罗斯':['russia','russian','kremlin','克里姆林宫'],
    '法国':['france','french','paris','巴黎'], '波兰':['poland','polish','warsaw','华沙'],
    '政变':['coup','putsch','夺权','军事接管'], '集会':['rally','rallies','demonstration','集会'],
    '示威':['protest','protests','demonstrators'], '罢工':['on strike','strikes','workers strike'],
    '骚乱':['riots','unrest','riot'], '南海':['south china sea','west philippine sea','west ph sea'],
    '台海':['taiwan strait','台湾海峡'], '朝鲜半岛':['north korea','south korea','dprk','pyongyang','朝鲜','韩国'],
    '日本':['japan','japanese'], '海警':['coast guard','maritime patrol'], '军演':['military exercises','military exercise','naval drill','military drills'],
    '防务':['missile','defence','defense','military aid','军售','导弹'], '外交':['diplomatic','foreign minister','bilateral talks','使馆'],
    '援助':['military aid','aid to ukraine','support for ukraine','ukraine aid','us support'], '谈判':['negotiation','negotiations','peace talks','ceasefire','停火'],
}
PRESETS = [
    dict(name='特朗普活动',kind='人物',scope='特朗普',dimensions='',question='本人见了谁、作了哪些表态或决定？'),
    dict(name='俄乌战争',kind='长期事件',scope='俄乌战争，乌克兰，俄罗斯',dimensions='战场，空袭，防务，谈判，援助，drone，attack，troops，war，rare earth',question='战场、外交谈判和外援分别有哪些实质进展？'),
    dict(name='波兰政变线索核查',kind='线索核查',scope='波兰',dimensions='政变，宪制危机，constitutional crisis',question='最早出处与支持、否认、更正的证据是什么？'),
    dict(name='法国社会动员与骚动',kind='长期事件',scope='法国',dimensions='集会，罢工，示威，骚乱',question='参与者、地点、诉求和政府回应有哪些变化？'),
    dict(name='中国周边态势',kind='区域态势',scope='台海，南海，朝鲜半岛，日本',dimensions='海警，军演，防务，外交',question='哪些行动实际改变了地区态势？'),
]

def terms(value):
    return list(dict.fromkeys(t.strip().casefold() for t in re.split(r'[,，;；、\n]+', value) if t.strip()))

class Scope(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: Literal['人物','长期事件','区域态势','线索核查'] = '长期事件'
    scope: str = Field(min_length=1,max_length=600)
    dimensions: str = Field(default='',max_length=600)
    exclude: str = Field(default='',max_length=600)
    question: str = Field(default='',max_length=600)
    enabled: bool = True
    auto_research: bool = False
    review_days: int = Field(default=7,ge=1,le=90)
    @field_validator('name','scope','dimensions','exclude','question',mode='before')
    @classmethod
    def strip(cls,v):
        return v.strip() if isinstance(v,str) else v
    @model_validator(mode='after')
    def validate_terms(self):
        if not terms(self.scope):
            raise ValueError('请填写关注对象或地区')
        if any(len(terms(v))>32 for v in (self.scope,self.dimensions,self.exclude)):
            raise ValueError('每栏最多 32 项')
        if self.auto_research and not self.question:
            raise ValueError('自动补查需要明确的长期问题')
        return self

def norm(text):
    return unicodedata.normalize('NFKC',text or '').casefold()

def hits(keys, text):
    result=[]
    for key in keys:
        for phrase in (key,*ALIASES.get(key,())):
            needle=norm(phrase)
            if (needle in text and (not needle.isascii() or
                    re.search(r'(?<![a-z0-9])'+re.escape(needle)+r'(?![a-z0-9])',text))):
                result.append(key)
                break
    return result

def person_actions(w, card):
    # An exact argument alias can affect personal relevance. Surname substrings in
    # another person's full name and mere co-occurrence do not grant actor status.
    aliases = {norm(a) for key in terms(w.scope) for a in (key,*ALIASES.get(key,()))}
    return [m for article in json.loads(card.sources) for m in article.get('actions',[])
            if any(a['role'] in ('actor','speaker','participant') and norm(a['text']) in aliases
                   for a in m['arguments'])]


def match(w,card):
    text=card.headings+'\n'+card.material
    if hits(terms(w.exclude),text): return ''
    anchors=hits(terms(w.scope),text)
    if not anchors: return ''
    dims=terms(w.dimensions)
    matched=hits(dims,text)
    if dims and not matched:return ''
    if w.kind=='人物' and person_actions(w,card):
        return '角色命中 · '+'、'.join(anchors[:3]+matched[:3])
    background=w.kind=='人物' and not hits(anchors,card.headings)
    prefix='背景提及' if background else ('待核线索' if w.kind=='线索核查' else '范围命中')
    return prefix+' · '+'、'.join(anchors[:3]+matched[:3])

def focus_context(w,card,reason):
    """Topic relevance orders its own material independently of global impact."""
    if reason.startswith('待核'):
        return 2, '线索核查'
    if reason.startswith('背景'):
        return 0, '背景提及'
    in_title = hits(terms(w.scope), card.headings)
    dimensions = hits(terms(w.dimensions), card.headings) or hits(terms(w.dimensions), card.material)
    if w.kind == '人物':
        from ..extraction.schema import LABELS
        actions = person_actions(w,card)
        if actions:
            match = next((m for m in actions if m['status']=='reported'), actions[0])
            return (2 if match['status']=='reported' else 1), LABELS[match['kind']]+' · '+LABELS[match['status']]
        return 1, '标题或材料命中 · 角色待查'
    else:
        branch = '、'.join(dimensions[:3]) or '综合进展'
    return (2 if in_title else 1), branch

CHANGE=re.compile(r'签署|生效|撤回|更正|宣布|暂停|会谈|爆炸|袭击|停火|\b(signs?|signed|suspend\w*|postpon\w*|delays?|ceasefire|attack\w*|resign\w*|approv\w*|announc\w*|agrees?)\b',re.I)
TENTATIVE=re.compile(r'传闻|据称|可能|待证实|\b(alleg\w*|rumou?r\w*|might|could|unconfirmed)\b|[?？]',re.I)

IMPACT=re.compile(r'关税|停火|地震|疏散|制裁|侵入|空袭|供应中断|封锁|政变|戒严|条约|\b(tariffs?|earthquake|evacuat\w*|sanctions?|ceasefire|invasion|airstrike|blockade|treaty|coup|war)\b',re.I)
