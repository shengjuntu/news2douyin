from news2douyin.digest.action_groups import ActionSeatIndex


def card(key, title, **extra):
    return dict(key=key, title=title, **extra)


def test_recovery_actions_keep_second_changes_and_unrelated_objects():
    index = ActionSeatIndex()
    index.add(card('first', 'SpaceX successfully catches Starship and makes history'))
    for headline in ("SpaceX’s Starship returns from orbit and is caught by giant mechanical arms",
                     "SpaceX's Starship returns from orbit and is captured by launch pad arms in a dramatic first"):
        assert index.find(card('variant', headline))[0] == 'first'
    for headline in ('SpaceX catches Starship after orbit in historic first as booster explodes over Gulf',
                     'SpaceX successfully catches Skylark and makes history',
                     "SpaceX's Starship was caught by customs authorities",
                     'SpaceX will catch Starship', 'SpaceX says it caught Starship',
                     'A photo captures Starship as SpaceX postpones trial'):
        assert index.find(card('other', headline)) is None


def test_equipment_frame_is_not_brand_specific_and_does_not_chain():
    index = ActionSeatIndex()
    index.add(card('known-sea', "NorthPort's Comet returns from sea and is hoisted by a dockside crane"))
    possible = card('unknown-route', 'NorthPort lifts Comet')
    assert index.find(possible)[0] == 'known-sea'
    other = card('other', "NorthPort's Comet returns from flight and is lifted by an overhead crane")
    # The ambiguous "NorthPort lifts Comet" was matched, not indexed.
    assert index.find(other) is None
    index.add(other)
    assert index.find(possible) is None


def test_active_passive_and_licensed_verb_phrases_preserve_roles():
    index = ActionSeatIndex()
    index.add(card('bill', 'French Senate approves the Normandy rail project'))
    index.add(card('signed', 'Lima and Quito sign a fisheries pact'))
    for headline in ('The Normandy rail project was approved by the French Senate',
                     'French Senate gave its approval to the Normandy rail project',
                     'French Senate has approved Normandy rail project'):
        assert index.find(card('other', headline))[0] == 'bill'
    assert index.find(card('pact', 'Lima and Quito inked the fisheries pact'))[0] == 'signed'
    for headline in ('French Senate ratifies the Normandy rail project',
                     'French Senate approves the Lyon rail project',
                     'French Senate approves Normandy rail project on Monday',
                     'French Senate rejected Normandy rail project',
                     'French Senate may approve a Normandy rail project',
                     'French Senate approves Normandy rail project?',
                     'French Senate denies it approved Normandy rail project',
                     'French Senate approves Normandy rail project and launches tender',
                     'French Senate approves Normandy rail project if funds arrive',
                     'The Normandy rail project approves the French Senate',
                     'Lima and Quito suspended the fisheries pact'):
        assert index.find(card('keep', headline)) is None


def test_amounts_sources_and_different_actions():
    index = ActionSeatIndex()
    index.add(card('seed', 'Ontario approves €5.2m for Ottawa shelters',
                   source_url='https://press.example/a'))
    for title in ('Ontario approved $5.2m for Ottawa shelters',
                  'Ontario approved €52m for Ottawa shelters', 'Ontario approves €5.2m for London shelters',
                  'Ontario approves €5.2m from Ottawa shelters', 'Ontario closes Ottawa shelters'):
        assert index.find(card('keep', title, source_url='https://press.example/a')) is None
    index.add(card('evac', 'Springfield orders evacuation near ethanol plant'))
    assert index.find(card('lift', 'Springfield lifts evacuation order near ethanol plant')) is None
    index.add(card('opening', 'Manila opens summit of attending ministers'))
    assert index.find(card('pact', 'Ministers sign agreement during Manila summit')) is None


def test_explicit_evidence_conflicts_and_incomplete_titles():
    index = ActionSeatIndex()
    index.add(card('anchor', 'The Normandy rail project was approved by French Senate',
                   source_kind='gdelt', official_coverage={'event_id':'100'}, actors=['Senate'],
                   country='FR', source_clock={'event_day':'2026-11-10'}))
    title = 'French Senate has approved Normandy rail project'
    for extra in ({'official_coverage': {'event_id': '101'}}, {'country': 'DE'},
                  {'actors': ['Different party']}, {'source_clock': {'event_day':'2026-11-11'}},
                  {'reconstruction': {'status':'lead'}}):
        assert index.find(card('b', title, source_kind='gdelt', **extra)) is None
    assert index.find(card('b', title, source_kind='gkg', country='DE'))[0] == 'anchor'
    index.add(card('blank', ''))
    index.add(card('generic', 'Police arrest a man'))
    assert index.find(card('blank2', '')) is None
    assert index.find(card('generic2', 'Police arrested a man')) is None


def test_ending_an_order_needs_same_incident_place_and_location_relation():
    index = ActionSeatIndex()
    index.add(card('order', 'Police lift evacuation after bomb found near ethanol plant in central Illinois'))
    for t in ('Police lift evacuation after bomb found near Illinois ethanol plant',
              'Evacuation order lifted after bomb found near Illinois ethanol plant'):
        assert index.find(card('alt', t))[0] == 'order'
    for t in ('Police order evacuation after bomb found near Illinois ethanol plant',
              'Police lift evacuation after bomb found at Illinois ethanol plant',
              'Police lift evacuation after bomb found near ethanol plant in northern Illinois',
              'Authorities lift evacuation after bomb found near Illinois ethanol plant',
              'Police lift evacuation after leak detected near Illinois ethanol plant',
              'Police lift curfew after bomb found near Illinois ethanol plant',
              'Police lift evacuation after bomb found near Illinois oil terminal'):
        assert index.find(card('keep', t)) is None
    # A different type of alert and facility use the same grammar.
    index.add(card('alert', 'Authorities lift boil-water advisory after leak detected outside iron refinery in Dublin'))
    assert index.find(card('alternate', 'Boil-water advisory lifted after leak detected outside Dublin iron refinery'))[0] == 'alert'


def test_plan_as_approved_object_and_a_future_action_do_not_share_a_seat():
    index = ActionSeatIndex()
    index.add(card('approved', 'Northgate Council approves the coastal transit plan'))
    assert index.find(card('same', 'The coastal transit plan was approved by Northgate Council'))[0] == 'approved'
    assert index.find(card('future', 'Northgate Council plans to approve the coastal transit plan')) is None
