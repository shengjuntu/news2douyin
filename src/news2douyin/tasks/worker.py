from __future__ import annotations

import threading
from loguru import logger
from sqlmodel import Session

from ..storage.utils import loads
from .control import TaskCancelled, TaskLeaseLost, TaskWorkerStopping
from .service import TaskContext, TaskService


class TaskWorker:
    """One collection, script or video task per process; leases support peers."""
    def __init__(self, engine, storage_root='runs_v7', *, lease_seconds=30, poll_seconds=0.5):
        if lease_seconds <= 0 or poll_seconds <= 0:
            raise ValueError('worker intervals must be positive')
        self.service = TaskService(engine)
        self.storage_root = storage_root
        self.lease_seconds, self.poll_seconds = lease_seconds, poll_seconds
        self._stop = threading.Event()
        self._thread = None

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name='news2douyin-worker')
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.service.recover()
                task = self.service.claim(self.lease_seconds)
                if task:
                    self.execute(task)
                    continue
            except Exception:
                logger.exception('Task worker iteration failed')
            self._stop.wait(self.poll_seconds)

    def execute(self, task):
        from ..collect.service import run_collection
        context = TaskContext(self.service, task, self._stop)
        done = threading.Event()

        def heartbeat():
            while not done.wait(self.lease_seconds / 3):
                try:
                    if not self.service.renew(task.task_id, task.lease_owner, self.lease_seconds):
                        return
                except Exception:
                    # Never extend a lease after it expires. The pipeline fences
                    # every business write even if this thread cannot reach DB.
                    logger.exception('Task heartbeat failed')

        thread = threading.Thread(target=heartbeat, daemon=True, name='news2douyin-heartbeat')
        thread.start()
        try:
            if task.trigger_type == 'video':
                from ..video.render import run_video
                run_video(self.service.engine, task, self.storage_root, context)
            elif task.trigger_type == 'script':
                from ..editorial.generation import run_generation
                run_generation(self.service.engine, task, self.storage_root, context)
            else:
                with Session(self.service.engine) as session:
                    run_collection(session, task.profile_name, storage_root=self.storage_root,
                                   trigger_type=task.trigger_type, job_id=task.job_id,
                                   profile_snapshot=loads(task.profile_json, {}), control=context)
        except TaskLeaseLost:
            logger.warning(f'Task ownership lost: {task.task_id}')
        except Exception as exc:
            try:
                if isinstance(exc, TaskCancelled):
                    context.terminate('cancelled', str(exc))
                elif isinstance(exc, TaskWorkerStopping):
                    context.terminate('queued', str(exc), interrupted=True)
                else:
                    context.terminate('failed', f'{type(exc).__name__}: {exc}'[:4000])
                    logger.exception(f'Task failed: {task.task_id}')
            except TaskLeaseLost:
                logger.warning(f'Task recovered by another worker: {task.task_id}')
        finally:
            done.set()
            thread.join(timeout=5)
