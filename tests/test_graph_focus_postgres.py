"""Run public topic-worker behavior against PostgreSQL and real FalkorDB.

The same behavioral contracts as the SQLite suite apply. The QA launcher
chooses the database locale; no configured production database is opened.
"""
import pytest

from news2douyin.server.app import create_app
from test_content_foundation import live_graph
from test_postgres_integration import postgres_url
import test_graph_focus_incremental as behavior


@pytest.mark.parametrize('scenario', [
    behavior.test_topic_material_is_bounded_and_an_event_can_run_between_batches,
    behavior.test_one_updated_material_does_not_rewrite_unchanged_topic_events,
    behavior.test_retokenizing_a_topic_each_tick_does_not_reset_its_material_cursor,
    behavior.test_topic_restart_resumes_committed_material_without_rewriting_it,
    behavior.test_excluding_already_projected_member_during_a_pass_converges,
    behavior.test_graph_write_before_checkpoint_failure_is_replayed_safely,
], ids=['bounded-and-fair', 'changed-only', 'continuous-updates', 'durable-restart',
        'remove-member', 'crash-replay'])
def test_postgres_topic_worker_contract(scenario, postgres_url, live_graph, tmp_path, monkeypatch):
    monkeypatch.setattr(behavior, 'app_at', lambda path: create_app(
        db_url=postgres_url, storage_root=str(path/'runs')))
    scenario(tmp_path, live_graph, monkeypatch)
    # Preserve fields used by the older full-topic writer during rollback.
    for graph in live_graph.scan_iter(match='test_live_focus_*'):
        missing = live_graph.execute_command('GRAPH.RO_QUERY', graph,
            'MATCH (e:Event) WHERE e.stamp IS NULL RETURN count(e)')[1]
        assert missing == [[0]]
    missing = live_graph.execute_command('GRAPH.RO_QUERY', 'test_live_active_global',
        'MATCH (:Focus)-[r:TRACKS]->() WHERE r.stamp IS NULL RETURN count(r)')[1]
    assert missing == [[0]]
