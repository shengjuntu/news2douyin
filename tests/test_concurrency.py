from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlmodel import Session, select

from news2douyin.collect import service as collect
from news2douyin.scheduler import service as scheduling
from news2douyin.storage.models import Article, ArticleEventLink, CollectJob, Event, ScheduleOccurrence
from conftest import FrozenTime


def test_overlapping_collectors_do_not_fail_or_duplicate_articles(engine, article, monkeypatch, tmp_path):
    with Session(engine) as session:
        collect.create_or_update_profile(session, {'name': 'fixture', 'provider': 'fixture'})
    monkeypatch.setitem(collect.PROVIDERS, 'fixture', lambda _: [dict(article)])
    decide = collect.decide_duplicate
    barrier = Barrier(2)

    def decide_at_same_time(*args):
        result = decide(*args)
        barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(collect, 'decide_duplicate', decide_at_same_time)

    def run():
        with Session(engine) as session:
            return collect.run_collection(session, 'fixture', storage_root=tmp_path / 'runs').status

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run) for _ in range(2)]
        assert [f.result(timeout=10) for f in futures] == ['succeeded', 'succeeded']
    with Session(engine) as session:
        assert len(list(session.exec(select(Article)))) == 1
        assert len(list(session.exec(select(ArticleEventLink)))) == 1
        assert session.exec(select(Event)).one().article_count == 1


def test_two_scheduler_instances_claim_once(engine, monkeypatch):
    with Session(engine) as session:
        job = CollectJob(name='scheduled', profile_name='fixture', cron_expr='* * * * *')
        session.add(job)
        session.commit()
    monkeypatch.setattr(scheduling, 'datetime', FrozenTime)
    calls = []
    monkeypatch.setattr(scheduling.SchedulerService, '_run_job', lambda self, job_id, key: calls.append(key))
    barrier = Barrier(2)

    def run():
        worker = scheduling.SchedulerService(engine)
        barrier.wait(timeout=5)
        worker.tick()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)
    assert len(calls) == 1
    with Session(engine) as session:
        assert len(list(session.exec(select(ScheduleOccurrence)))) == 1
