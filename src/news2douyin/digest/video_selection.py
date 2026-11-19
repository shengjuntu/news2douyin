"""Choose short-video material by cues in the saved report, independently of Daily Top 100."""
from __future__ import annotations

import re

from ..dedup.normalize import canonicalize_url
from .candidate_pool import grouped_candidates
from .headline_identity import progress_keys

STRATEGY_ID = 'video_story'
VERSION = '1.0.0'


def _words(words):
    return rf'(?<!\w)(?:{words})(?!\w)'


_PERSON = re.compile(
    r'夫妻|父母|母亲|妈妈|爸爸|父亲|爷爷|奶奶|祖父|祖母|兄弟|姐妹|女孩|男孩|孩子|老人|'
    r'租客|邻居|住户|业主|乘客|通勤者|顾客|患者|病患|家长|学生|教师|老师|护士|医生|'
    r'司机|外卖员|骑手|快递员|店主|店员|摊主|工人|农民|渔民|志愿者|消防员|维修员|'
    r'退休人员|居民|打工人|职工|清洁工|保洁' + '|' + _words(
        r'parents?|moth(?:er|ers)|fathers?|grandmothers?|grandfathers?|siblings?|children|child|'
        r'girls?|boys?|neighbors?|neighbours?|tenants?|passengers?|commuters?|customers?|patients?|'
        r'students?|teachers?|nurses?|doctors?|drivers?|couriers?|shopkeepers?|vendors?|'
        r'workers?|farmers?|fisher(?:men|man|women|woman)|volunteers?|firefighters?|mechanics?|'
        r'retirees?|residents?|chauffeurs?|élèves|enseignant(?:e|es|s)?|voisin(?:e|es|s)?|'
        r'madres?|padres?|vecin(?:o|os|a|as)|maestr(?:o|os|a|as)|conductores?|conductora(?:s)?|'
        r'motoristas?|morador(?:a|es|as)?|professor(?:a|es|as)?'
    ), re.I,
)

_ROUNDUP = re.compile(
    r'^(?:今日要闻|新闻早报|要闻速览|要闻回顾|新闻简报|'
    r'news roundup\b|morning briefing\b|\d+ things to know\b)', re.I,
)

_ACTION = re.compile(
    r'停课|复课|上课|开学|搬家|搬离|搬回|赶到|掉头|绕行|改道|恢复|复工|重开|停业|关闭|'
    r'开设|暂停|取消|推迟|接回|找回|找到|发现|获救|救下|救出|送回|送到|送饭|帮忙|'
    r'捐出|捐助|帮扶|道歉|赔偿|退款|退费|退还|减免|涨价|降价|上涨|下降|加薪|降薪|停工|罢工|'
    r'开通|开行|增加|减少|缩短|延长|换乘|排队|停运|失业|歇业|辞职|失踪|落户|获奖|'
    r'生效|开始实施|通过决议|辞退|关停|修复|检修|改签|支付|买到|收到|组成|筹钱|获准|'
    r'免费发放|获赔|被困|回家|还给|接送|援助|接力|拦下|让座|送医|募款' + '|' + _words(
        r'reopens?|reopened|opened|opens?|shuts?|closed|closes?|stops?|stopped|'
        r'resumes?|resumed|returns?|returned|reunites?|reunited|rescues?|rescued|'
        r'saves?|saved|helps?|helped|refuses?|refused|reverses?|reversed|cancels?|cancell?ed|'
        r'restores?|restored|loses?|lost|receives?|received|offers?|offered|donates?|donated|'
        r'refunds?|refunded|repaid|repays?|paid|pays?|apologi[sz]es?|apologi[sz]ed|'
        r'walks?|walked|moves?|moved|switches?|switched|reroutes?|rerouted|redirects?|redirected|'
        r'quits?|fires?|fired|strikes?|struck|found|finds?|wins?|won|grants?|granted|'
        r'waives?|waived|cuts?|increases?|increased|rises?|rose|falls?|fell|charges?|charged|'
        r'takes? effect|took effect|opens? for traffic|pairs? up|raises?|raised|repairs?|repaired|'
        r'volvió|volvieron|rescat(?:ó|aron)|cerr(?:ó|aron)|abri(?:ó|eron)|ayud(?:ó|aron)|'
        r'regres(?:ó|aron)|suspendi(?:ó|eron)|cancel(?:ó|aron)|rouvr(?:e|ir|ent)|ferme|'
        r'revient|a retrouvé|secouru|reabriu|fechou|voltou|resgatou|ajudou'
    ), re.I,
)

_SEQUENCE = re.compile(
    r'随后|后来|之后|此前|之后又|最终|最后|却|反而|没想到|没料到|一开始|转而|如今|'
    r'(?:数|\d+|[一二三四五六七八九十两半])(?:天|周|个?月|年|小时|分钟)后' + '|' + _words(
        r'after|before|then|later|eventually|instead|once|previously|but|until|'
        r'después|luego|antes de|al final|après|avant de|finalement|depois|antes de|enfim'
    ), re.I,
)

_UNVERIFIED = re.compile(
    r'传闻|据称|声称|被指|待证实|可能|如果|或将|即将|将于|计划|预计|否认|尚未|并未|没有|'
    r'应该|应当|理应|必须|教你|如何才能|[?？]|n[’\']t\b' + '|' + _words(
        r'allegedly|alleged|reportedly|claim(?:s|ed)?|rumou?rs?|unconfirmed|could|might|will|'
        r'plans? to|would|is expected to|if|not|never|no evidence|must|should|needs? to|how to|'
        r'podría|supuestamente|se espera que|'
        r'planea|pourrait|prévoit|aurait|poderia'
    ), re.I,
)

_CLICKBAIT = re.compile(
    r'震惊全网|全网震惊|令人窒息|看完泪奔|必须看完|结局惊呆|网友炸锅' + '|' + _words(
        r'you won.t believe|what happened next will|this will shock you|must see|left everyone speechless'
    ), re.I,
)

_SENTENCES = re.compile(r'(?:[^。！？!?；;.\n]|\.(?!\s|$))+[。！？!?；;.]?')
_SECTION_SUFFIX = re.compile(r'\s+[|｜]\s+(?:local news|news|crime news|社会新闻|地方新闻)$', re.I)
_SERIOUS_HARM = re.compile(
    r'性侵|性虐待|强奸|猥亵|儿童色情|虐童|虐待儿童|分尸|肢解|碎尸|谋杀|枪杀|枪击|'
    r'遇害|被杀|尸体|尸首|遗体被发现' + '|' + _words(
        r'sex(?:ual)? (?:abuse|assault)|sex crimes?|explicit texts|child (?:abuse|pornography)|'
        r'child porn|rape|sex trafficking|murder(?:ed)?|homicide|dismember(?:ed|ment)|'
        r'body found|found dead|shooting|shot dead|pulled (?:a )?gun|stabbed|'
        r'abuso sexual|violación|assassin(?:é|ée|at)|agressão sexual'
    ), re.I,
)
_SERIOUS_ALLEGATION = re.compile(
    r'被控|涉嫌|指控|被捕|起诉|追查' + '|' + _words(
        r'charged with|charged after|(?:faces?|facing) (?:felony )?charges|indicted|charges filed|'
        r'under investigation|alleg(?:e|es|ed)|accused|arrested'
    ), re.I,
)
_HUMAN_OUTCOME = re.compile(
    r'送饭|接力|让座|帮忙|捐出|捐助|获救|救出|找回|送回|团聚|重聚|退费|退款|免学费|减免|复学|'
    r'获赔|赔偿|返还|无罪' + '|' + _words(
        r'helped|donates?|donated|rescued|reunites?|reunited|survives?|survived|'
        r'found alive|repaid|refunded|returned home|acquitted|exonerated|compensated|'
        r'free tuition|safe at home|returns? to class|returned to class|a salvo|rescat(?:ado|ada)|liber(?:ado|ada)'
    ), re.I,
)

_LIFE = tuple((label, re.compile(chinese + '|' + _words(latin), re.I)) for label, chinese, latin in (
    ('出行与通勤', '通勤|地铁|公交|班车|校车|车票|公交卡|站台|轮渡|接驳|停车费|上下班',
     r'commut(?:e|es|ing)|bus(?:es)?|subway|metro|school buses|ferr(?:y|ies)|bus fare|train fare|parking fees?|autob(?:ús|uses)|ônibus'),
    ('工作与收入', '上班|下班|工资|失业|加班|排班|工作岗位|打零工|薪水|薪资|送外卖|快递',
     r'wages?|paychecks?|pay cheques?|working hours|overtime|job losses|unemployment|gig work|delivery shifts?|salari(?:o|os)|sueldo'),
    ('居住与账单', '房租|租金|供暖|停水|停电|学区房|住房|搬离|水费|电费|燃气费|账单|物业',
     r'rent|housing|tenants?|utility bills?|electricity bills?|power cuts?|water bills?|heating bills?|water outage|rental costs?|alquiler|aluguel|loyer'),
    ('教育与照护', '学校|上学|上课|幼儿园|学费|食堂|课后|接孩子|养老|看病|就医|药费|住院|照护',
     r'schools?|classroom|tuition|school meals|child care|childcare|daycare|nursing homes?|home care|hospital visits?|prescription costs?|escuelas?|escolas?|crèche'),
    ('购物与饮食', '买菜|菜价|小卖部|食品价格|食材|超市|便利店|社区商店|外卖|热饭|餐费|伙食',
     r'grocer(?:ies|y)|supermarkets?|food prices|ordering food|food delivery|hot meal|community shops?|mercados?|supermercados?'),
    ('家庭与邻里', '邻里|邻居|团聚|一家人|家里|社区活动|接娃|独居|遛狗|收养|宠物',
     r'neighborhoods?|neighbourhoods?|reunions?|family meals?|families?|living alone|pets?|vecindario|vizinhança'),
))


def _clue(label, field, text, match):
    # Keep the display excerpt contiguous and within a single saved source field.
    left = max(0, match.start() - 66)
    right = min(len(text), max(left + 150, match.end()))
    return {'text': label, 'quote': text[left:right].strip(), 'field': field}


def _features(card):
    title = _SECTION_SUFFIX.sub('', card['title'])
    reconstructed = bool(card.get('reconstruction'))
    excerpt = card.get('excerpt', '') if not reconstructed else ''
    fields = [('title', title)]
    if excerpt.strip() and excerpt.strip() != title.strip():
        fields.append(('excerpt', excerpt))
    person = life = movement = None
    sequence = False
    for field, text in fields:
        if person is None and (actor := _PERSON.search(text)):
            person = _clue(actor.group(), field, text, actor)
        if life is None:
            for label, pattern in _LIFE:
                if match := pattern.search(text):
                    life = _clue(label, field, text, match)
                    break
        if not sequence:
            for sentence in _SENTENCES.finditer(text):
                segment = sentence.group()
                if _UNVERIFIED.search(segment):
                    continue
                action = _ACTION.search(text, sentence.start(), sentence.end())
                if not action:
                    continue
                progression = (_SEQUENCE.search(text, sentence.start(), sentence.end()) and
                               _ACTION.search(text, action.end(), sentence.end()))
                if progression:
                    movement = _clue('有前后变化或应对经过', field, text, progression)
                    sequence = True
                    break
                if movement is None:
                    movement = _clue('有具体行动或变化', field, text, action)
    if movement is None:
        evidence = card.get('action_evidence') or {}
        quote = evidence.get('quote', '')
        if evidence.get('kind') in ('dated_action_report', 'stage_report') and quote:
            for field, text in fields:
                if quote in text and not _UNVERIFIED.search(quote):
                    movement = {'text': '报道中有行动阶段', 'quote': quote, 'field': field}
                    break
    status = 'reconstruction' if reconstructed else 'excerpt' if len(fields) > 1 else 'headline'
    return person, movement, life, sequence, status


def _score(card, features):
    person, movement, life, sequence, status = features
    if not movement or not (person or life) or status == 'reconstruction' or _ROUNDUP.match(card['title']):
        return None
    title = card['title']
    outcome = _HUMAN_OUTCOME.search(movement['quote'])
    harm = _SERIOUS_HARM.search(title)
    # A headline-level allegation gets a video slot when it also reports a
    # concrete outcome for the people involved. Standalone graphic details
    # receive less weight, even when the report itself is a useful daily item.
    if harm and status == 'headline' and _SERIOUS_ALLEGATION.search(title) and not outcome:
        return None
    return (36 * bool(person) + 30 * bool(life) + 20 * sequence + 8 * (status == 'excerpt')
            + 10 * bool(outcome) - 36 * bool(harm and not outcome)
            - 14 * bool(_CLICKBAIT.search(title)))


_EVIDENCE_NAMES = {'excerpt': '有可读摘录', 'headline': '标题线索，制作前核对原文'}


def _publish(card, score):
    person, movement, life, _, status = _features(card)
    video = {key: clue for key, clue in (('person', person), ('movement', movement), ('life', life)) if clue}
    video['evidence'] = {'status': status, 'url': card['source_url'], 'source_key': card['source_key']}
    return dict(card, score=score, video=video,
                reasons=[clue['text'] for clue in (person, movement, life) if clue] + [_EVIDENCE_NAMES[status]])


def rank_video(pool, *, prior_keys=(), limit=100):
    """Read every group with bounded memory and suppress exact source and headline repeats."""
    if type(limit) is not int or limit < 0:
        raise ValueError('limit must be a nonnegative integer')
    prior = pool.current_keys(prior_keys)
    counters = {'eligible': 0, 'prior_excluded': 0, 'insufficient_story_evidence': 0}

    def scored():
        for card in pool.iter_cards():
            if card['key'] in prior:
                counters['prior_excluded'] += 1
                continue
            score = _score(card, _features(card))
            if score is None:
                counters['insufficient_story_evidence'] += 1
                continue
            counters['eligible'] += 1
            headline = _SECTION_SUFFIX.sub('', card['title'])
            alias = progress_keys(headline, card['source_url'])[1] if headline != card['title'] else card['key']
            yield canonicalize_url(card['source_url']), card['key'], alias or card['key'], score, card['observed_at']

    db = pool.db
    db.execute('CREATE TEMP TABLE video_scores (url TEXT PRIMARY KEY,key TEXT,alias TEXT,score INTEGER,observed TEXT)')
    try:
        db.executemany('''INSERT INTO video_scores VALUES (?,?,?,?,?) ON CONFLICT(url) DO UPDATE
                          SET key=excluded.key,alias=excluded.alias,score=excluded.score,observed=excluded.observed
                          WHERE (excluded.score,excluded.observed) > (video_scores.score,video_scores.observed)
                          OR ((excluded.score,excluded.observed) = (video_scores.score,video_scores.observed)
                              AND excluded.key < video_scores.key)''', scored())
        url_count, alias_count = db.execute('SELECT count(*),count(DISTINCT alias) FROM video_scores').fetchone()
        selected = db.execute('''WITH unique_titles AS (
                                   SELECT key,score,observed,row_number() OVER (
                                     PARTITION BY alias ORDER BY score DESC,observed DESC,key
                                   ) AS occurrence FROM video_scores
                                 )
                                 SELECT key,score FROM unique_titles WHERE occurrence=1
                                 ORDER BY score DESC,observed DESC,key LIMIT ?''',
                              (min(limit, alias_count),)).fetchall()
        cards = pool.current_cards(key for key, _ in selected)
        items = [_publish(cards[key], score) for key, score in selected]
    finally:
        db.execute('DROP TABLE video_scores')
    return dict(items=items, stats=dict(pool.stats, **counters, selected=len(items), mode=STRATEGY_ID,
                                       duplicate_source_groups=counters['eligible'] - url_count,
                                       duplicate_title_groups=url_count - alias_count))


def select_video(candidates, *, prior_keys=(), limit=100):
    with grouped_candidates(candidates, keep_baseline=False) as pool:
        return rank_video(pool, prior_keys=prior_keys, limit=limit)
