"""Advance durable topic queues explicitly when app lifespan is not running."""


def drain_focus(engine, limit=100):
    from news2douyin.desk.focus_jobs import step
    for _ in range(limit):
        if not step(engine):
            return
    raise AssertionError('topic processing did not settle within bounded fixture ticks')
