import importlib.util
from hashlib import sha256
from pathlib import Path

import pytest
from news2douyin.selection.ngram_actions import diagnose_excerpt, match_excerpt
from news2douyin.intake import ngrams
from test_ngram_provider import compressed, tokens
from test_official_events import event_line, mention_line, zipped


@pytest.mark.parametrize('body,language,left,right,code,reason', [
    ('', 'en', 'France', 'Poland', '04', 'empty_text'),
    ('Un texto.', 'es', 'France', 'Poland', '04', 'language_outside_rule'),
    ('Text.', 'en', 'France', '', '04', 'actor_missing'),
    ('Text.', 'en', 'France', 'FRANCE', '04', 'actors_identical'),
    ('Text.', 'en', 'Police', 'France', '04', 'actor_generic'),
    ('Text.', 'en', 'US', 'France', '04', 'actor_too_short'),
    ('Text.', 'en', 'France', 'Poland', '01', 'root_without_rule'),
    ('France met with Russia.', 'en', 'France', 'Poland', '04', 'actor_literals_missing'),
    ('France met there. Poland hosted.', 'en', 'France', 'Poland', '04', 'actors_separate'),
    ('France met with Poland '+ 'x'*501, 'en', 'France', 'Poland', '04', 'only_long_sentence'),
    ('Officials in France and Poland spoke.', 'en', 'France', 'Poland', '04', 'cue_absent'),
    ('France never met with Poland.', 'en', 'France', 'Poland', '04', 'guarded_wording'),
    ('Poland met with France. France and Poland attended.', 'en', 'France', 'Poland', '04', 'unqualified_roles'),
    ('France met with Poland.', 'en', 'France', 'Poland', '04', 'matched'),
])
def test_action_decision_identifies_specific_gate(body, language, left, right, code, reason):
    d = diagnose_excerpt(body, language, left, right, code)
    assert d.reason == reason
    assert bool(d.anchors) == (reason == 'matched')
    assert match_excerpt(body, language, left, right, code) == (d.excerpt, d.anchors)


def test_audit_reads_local_archives_once_and_keeps_default_output_free_of_text_and_urls(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / 'tools/audit_ngram_actions.py'
    spec = importlib.util.spec_from_file_location('audit_ngram_actions', path)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    url = 'https://publisher.example/city'
    event_path = tmp_path/'20261113001500.export.CSV.zip'
    mentions_path = tmp_path/'20261113001500.mentions.CSV.zip'
    minute_path = tmp_path/'20261113000100.webngrams.json.gz'
    event_path.write_bytes(zipped([event_line(code='040')]))
    mentions_path.write_bytes(zipped([mention_line(url=url), mention_line(url=url)], feed='mentions_english'))
    minute_path.write_bytes(compressed([tokens('China met with the United States.', '', '', url=url)]))
    monkeypatch.setattr(ngrams, '_load_assembler', lambda: lambda url, rows: rows[0]['ngram'])
    monkeypatch.setattr(ngrams, '_download', lambda *a, **kw: pytest.fail('unexpected HTTP'))
    public, private = tool.audit(event_path, mentions_path, minute_path, private=True)
    assert public['mention_rows']['valid'] == 2 and public['paired_records'] == public['passed'] == 1
    assert public['pair_sample'][0]['pair_sha256'] == sha256(('001234567\n'+url).encode()).hexdigest()
    assert 'publisher.example' not in str(public) and 'met with the United States' not in str(public)
    assert private[0]['reconstructed_text'] == 'China met with the United States.'
    public_only, hidden = tool.audit(event_path, mentions_path, minute_path)
    assert public == public_only and not hidden
    minute_path.write_bytes(b'bad')
    with pytest.raises(OSError):
        tool.audit(event_path, mentions_path, minute_path)
    assert minute_path.read_bytes() == b'bad'


def test_later_complete_sentence_can_qualify_when_first_one_is_attributed():
    found = diagnose_excerpt('France reportedly met with Poland. France also met with Poland.',
                             'en', 'France', 'Poland', '04')
    assert found.reason == 'matched' and found.excerpt == 'France also met with Poland.'
