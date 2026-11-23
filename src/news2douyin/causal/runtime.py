"""Run investigations independently from the collection and content queue."""
import logging
from threading import Event, Thread

from .model import CausalModelError, configure
from .service import CausalLeaseLost, claim, complete, investigate
from . import tree_worker

log = logging.getLogger(__name__)


class CausalWorker:
    def __init__(self, engine, storage_root=None):
        self.engine = engine
        self.storage_root = storage_root
        self.stop_event = Event()
        self.wake_event = Event()
        self.thread = None

    def step(self):
        job = claim(self.engine)
        if job is None:
            return False
        try:
            if getattr(job, 'program', False):
                if self.storage_root is None:
                    step = tree_worker.investigate(self.engine, job)
                else:
                    step = tree_worker.investigate(self.engine, job, storage_root=self.storage_root)
            else:
                results = investigate(self.engine, job)
        except CausalLeaseLost:
            return True
        except tree_worker.ProgramStepFailure as exc:
            complete(self.engine, job, error=str(exc), queries=exc.queries,
                     candidate_count=exc.candidate_count, stop_reason=str(exc),
                     retrieval_mode=exc.retrieval_mode, retrieval_report=exc.retrieval_report)
        except CausalModelError as exc:
            complete(self.engine, job, error=str(exc))
        except Exception as exc:
            log.warning('Causal investigation lookup failed (%s)', type(exc).__name__)
            complete(self.engine, job, error='本轮本地检索失败，稍后可重试')
        else:
            if getattr(job, 'program', False):
                complete(self.engine, job, step.results, queries=step.queries,
                         candidate_count=step.candidate_count, stop_reason=step.stop_reason,
                         retrieval_mode=step.retrieval_mode, retrieval_report=step.retrieval_report)
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
