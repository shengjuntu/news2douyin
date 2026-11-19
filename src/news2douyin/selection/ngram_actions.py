"""Literal English action-sentence checks for reconstructed GDELT reports."""
import re
from typing import NamedTuple

# Conservative positive vocabulary for reconstructed English text, scoped by
# CAMEO family. Codes without a reliable cue can still yield document-only evidence.
_CUES = {code: re.compile(r'\b(?:' + pattern + r')\b', re.I) for code, pattern in {
    '02': r'appeal(?:ed|s)?|urg(?:ed|es)',
    '04': r'met with|held talks with|held negotiations with|negotiated with|consulted with',
    '05': r'signed (?:an?|the) (?:agreement|treaty|memorandum)',
    '07': r'provided aid|delivered aid|sent aid|provided assistance',
    '09': r'investigated|opened an investigation into',
    '10': r'demanded|issued an ultimatum',
    '11': r'criticized|criticised|condemned',
    '12': r'rejected|refused',
    '13': r'threatened|issued a threat',
    '14': r'protested against|demonstrated against',
    '16': r'severed ties with|cut ties with|recalled its ambassador from',
    '17': r'sanctioned|imposed sanctions on',
    '18': r'assaulted|attacked',
    '19': r'fought|clashed with|exchanged fire with',
}.items()}
_NON_ASSERTED = re.compile(
    r"\b(?:no|not|never|den(?:y|ied|ies|ying)|rejects? reports|untrue|false|alleged(?:ly)?|"
    r"if|unless|possibly|reportedly|rumou?red|may|might|could|would|should|will|plans?|planned|"
    r"hopes?|intends?|expected|scheduled|proposed|consider(?:s|ed|ing)?|claims?|claimed|says?|said|according to)\b|n['’]t\b|\?",
    re.I)
_SIMPLE_PREFIX = re.compile(r'(?:\s+(?:has|had|just|also|formally|officially|recently|publicly))*\s+', re.I)
_SIMPLE_OBJECT = re.compile(r'\s+(?:(?:to|on|with|against|at)\s+)?(?:the\s+)?', re.I)
_GENERIC_ACTORS = {'government', 'official', 'officials', 'citizen', 'citizens', 'police', 'military',
                   'rebel', 'rebels', 'president', 'minister', 'opposition', 'unknown', 'army'}


# Codes describe the literal decision, never the truth of an event or the fidelity
# of the reconstruction. Keep these stable for offline comparison across batches.
REASON_LABELS = {
    'empty_text': '没有可供核对的重建文字。',
    'language_outside_rule': '目前仅用英文句式自动核对行动。',
    'actor_missing': '官方编码缺少一方名称。',
    'actors_identical': '官方编码的双方名称相同。',
    'actor_generic': '官方一方使用通用身份，无法用它单独核对角色。',
    'actor_too_short': '一方名称短于三个字符，自动核对容易误认。',
    'root_without_rule': '这个 CAMEO 行动大类尚未配有英文动作词规则。',
    'actor_literals_missing': '重建文字中未检出双方完整名称。',
    'actors_separate': '双方名称出现在不同句子里。',
    'only_long_sentence': '含双方名称的句子都超过 500 字符。',
    'cue_absent': '含双方名称的短句未命中该行动大类的用词。',
    'guarded_wording': '相关句子带有否定、假设、转述或其他需要核实的措辞。',
    'unqualified_roles': '双方和行动词在同一句，但未能按当前句式核对动作方向。',
    'matched': '重建句中同时出现双方名称和同类行动用词，角色及事实仍待原文核实。',
}


class ActionDecision(NamedTuple):
    excerpt: str
    anchors: list[str]
    reason: str


def action_fields_reason(actor1, actor2, root_code):
    left = (actor1 or '').strip().casefold()
    right = (actor2 or '').strip().casefold()
    if not left or not right:
        return 'actor_missing'
    if left == right:
        return 'actors_identical'
    if left in _GENERIC_ACTORS or right in _GENERIC_ACTORS:
        return 'actor_generic'
    if min(len(left), len(right)) < 3:
        return 'actor_too_short'
    if root_code not in _CUES:
        return 'root_without_rule'
    return ''


def diagnose_excerpt(body, language, actor1, actor2, root_code):
    """Report the closest short sentence's check. Preserve the original lexical gate."""
    compact = ' '.join((body or '').split())
    preview = compact[:500]
    if not compact:
        return ActionDecision('', [], 'empty_text')
    if (language or '').lower().split('-')[0] not in ('en', 'eng', 'english'):
        return ActionDecision(preview, [], 'language_outside_rule')
    field_reason = action_fields_reason(actor1, actor2, root_code)
    if field_reason:
        return ActionDecision(preview, [], field_reason)
    cue = _CUES[root_code]
    parties = [re.compile(r'(?<!\w)' + re.escape(name.strip()) + r'(?!\w)', re.I) for name in (actor1, actor2)]
    if not all(p.search(compact) for p in parties):
        return ActionDecision(preview, [], 'actor_literals_missing')
    # A later weak sentence cannot erase a more informative reason seen earlier.
    stages = ('actors_separate', 'only_long_sentence', 'cue_absent', 'guarded_wording', 'unqualified_roles')
    stage = 0
    for sentence in re.split(r'(?<=[.!?])\s+', compact):
        subject, obj = (p.search(sentence) for p in parties)
        if not subject or not obj:
            continue
        if len(sentence) > 500:
            stage = max(stage, 1)
            continue
        if not cue.search(sentence):
            stage = max(stage, 2)
            continue
        if _NON_ASSERTED.search(sentence):
            stage = max(stage, 3)
            continue
        stage = 4
        if sentence[:subject.start()].strip().casefold() not in ('', 'the'):
            continue
        for action in cue.finditer(sentence):
            if (subject.end() < action.start() < action.end() < obj.start()
                    and _SIMPLE_PREFIX.fullmatch(sentence[subject.end():action.start()])
                    and _SIMPLE_OBJECT.fullmatch(sentence[action.end():obj.start()])):
                return ActionDecision(sentence, [subject.group(), obj.group(), action.group()], 'matched')
    return ActionDecision(preview, [], stages[stage])


def match_excerpt(body, language, actor1, actor2, root_code):
    """Return the original (excerpt, literal anchors) pair used by existing callers."""
    decision = diagnose_excerpt(body, language, actor1, actor2, root_code)
    return decision.excerpt, decision.anchors
