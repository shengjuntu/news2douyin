"""Run investigations independently from the collection and content queue."""
import logging
from threading import Event, Thread

from .model import CausalModelError, configure
from .service import claim, complete, investigate

log = logging.getLogger(__name__)


class CausalWorker:
    def __init__(self, engine):
        self.engine = engine
        self.stop_event = Event()
        self.wake_event = Event()
        self.thread = None

    def step(self):
        job = claim(self.engine)
        if job is None:
            return False
        try:
            results = investigate(self.engine, job)
        except CausalModelError as exc:
            complete(self.engine, job, error=str(exc))
        except Exception as exc:
            log.warning('Causal investigation lookup failed (%s)', type(exc).__name__)
            complete(self.engine, job, error='本轮本地检索失败，稍后可重试')
        else:
            complete(self.engine, job, results)
        return True

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                worked = self.step()
            except Exception as exc:
                log.warning('Causal investigation worker waiting to retry (%s)', type(exc).__name__)
                delay = 5
            else:
                if worked:
                    continue
                delay = 4
            self.wake_event.wait(delay)
            self.wake_event.clear()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        try:
            configure()
        except CausalModelError:
            return
        self.stop_event.clear()
        self.wake_event.clear()
        self.thread = Thread(target=self._loop, name='causal-investigations', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake_event.set()
        if self.thread:
            self.thread.join(5)

    def wake(self):
        self.wake_event.set()
