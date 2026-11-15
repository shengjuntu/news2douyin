"""Run SQL-backed attention graph queue contracts on actual PG + FalkorDB.

The QA PostgreSQL launcher supplies C/en_US clusters. Each case owns a random
database and disposable loopback Falkor server; no deployed graph is used.
"""
import pytest
from functools import partial

from news2douyin.server.app import create_app
from test_content_foundation import live_graph
from test_postgres_integration import postgres_url
import test_attention_graph as behavior


@pytest.mark.parametrize('scenario', [
    behavior.test_unknowns_are_unknown_and_overlay_is_only_in_existing_global_graph,
    behavior.test_idempotent_bounded_items_and_source_match_lifecycle,
    behavior.test_focus_recall_never_accepts_material_or_revives_archived_focus,
    behavior.test_missing_topic_cleanup_only_removes_its_overlay,
    behavior.test_attention_queue_cursor_and_token_fencing,
    partial(behavior.test_attention_gets_a_turn_under_continuous_stale_event_work, with_watch=True),
    partial(behavior.test_attention_gets_a_turn_under_continuous_stale_event_work, with_watch=False),
    behavior.test_attention_projection_sql_and_cypher_inputs_are_bounded,
], ids=['unknowns-and-indexes', 'bounded-idempotent-source-links', 'focus-invalidation',
        'missing-topic-cleanup', 'queue-cursor-and-fencing', 'queue-fairness-with-watch',
        'queue-fairness-without-watch', 'bounded-sql-and-cypher'])
def test_postgres_attention_graph_contract(scenario, postgres_url, live_graph, tmp_path, monkeypatch):
    monkeypatch.setattr(behavior, 'app_at', lambda path: create_app(
        db_url=postgres_url, storage_root=str(path/'runs')))
    scenario(tmp_path, live_graph, monkeypatch)
