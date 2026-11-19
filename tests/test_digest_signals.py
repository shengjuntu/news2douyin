from news2douyin.digest.signals import headline_signals


def test_single_issuer_document_with_no_popularity_can_be_identified():
    headline = 'New battery export controls enter into force'
    assert headline_signals(headline, 'https://eur-lex.europa.eu/eli/reg/2026/123/oj') == (1, True)
    assert headline_signals(headline, 'https://eur-lex.europa.eu.news.example/eli/reg/2026/123/oj') == (1, False)
    assert headline_signals(headline, 'https://www.gov.cn/xinwen/news.html') == (1, False)
    assert headline_signals('Un titre de page', 'https://www.example.fr/news/4') == (0, False)


def test_action_passage_requires_its_own_date_and_resolved_wording():
    from news2douyin.digest.signals import action_passage
    args = ('https://example.org/report', '2026-06-19')
    assert action_passage('Daily report','On 2026-06-19 the council ratified the treaty.',*args)['kind'] == 'dated_action_report'
    assert action_passage('Council ratified pact on June 19, 2026','',*args)['kind'] == 'dated_action_report'
    assert action_passage('Council ratified pact on 19 June 2026','',*args)['kind'] == 'dated_action_report'
    assert not action_passage('Council may have ratified pact on June 19, 2026','',*args)
    assert action_passage('委员会正式生效通告','',*args)['kind'] == 'stage_report'
    assert not action_passage('Council news','2026-06-19. Council ratified the treaty.',*args)
    assert not action_passage('Council news','It was not ratified on 2026-06-19.',*args)
    assert not action_passage('Council news','Previously, on 2026-06-19, the council ratified the treaty.',*args)
