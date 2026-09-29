from __future__ import annotations

from typing import Any
import time
from uuid import uuid4

import requests


class Client:
    def __init__(self, base_url: str = 'http://127.0.0.1:18080') -> None:
        self.base_url = base_url.rstrip('/')

    def get(self, path: str, *, _timeout: float = 60, **params):
        resp = requests.get(self.base_url + path, params=params, timeout=_timeout)
        resp.raise_for_status()
        return resp.json()

    def post(self, path: str, payload: dict[str, Any] | None = None, **params):
        resp = requests.post(self.base_url + path, json=payload, params=params, timeout=300)
        resp.raise_for_status()
        return resp.json()

    def health(self):
        return self.get('/api/health')

    def system_status(self):
        return self.get('/api/system/status')

    def scheduler_status(self):
        return self.get('/api/scheduler/status')

    def list_profiles(self):
        return self.get('/api/profiles')

    def save_profile(self, payload: dict[str, Any]):
        return self.post('/api/profiles', payload)

    def get_profile(self, name: str):
        return self.get(f'/api/profiles/{name}')

    def list_jobs(self):
        return self.get('/api/jobs')

    def save_job(self, payload: dict[str, Any]):
        return self.post('/api/jobs', payload)

    def enable_job(self, job_id: int):
        return self.post(f'/api/jobs/{job_id}/enable', {})

    def disable_job(self, job_id: int):
        return self.post(f'/api/jobs/{job_id}/disable', {})

    def submit_collection(self, profile_name: str, override=None, *, idempotency_key=None):
        return self.post('/api/tasks/collect', {'profile_name': profile_name,
                         'override': override or {}, 'idempotency_key': idempotency_key})

    def get_task(self, task_id: str, *, timeout: float = 60):
        return self.get(f'/api/tasks/{task_id}', _timeout=timeout)

    def list_tasks(self, limit: int = 50):
        return self.get('/api/tasks', limit=limit)

    def task_events(self, task_id: str, after: int = 0, limit: int = 200):
        return self.get(f'/api/tasks/{task_id}/events', after=after, limit=limit)

    def cancel_task(self, task_id: str):
        return self.post(f'/api/tasks/{task_id}/cancel')

    def retry_task(self, task_id: str):
        return self.post(f'/api/tasks/{task_id}/retry')

    def wait_task(self, task_id: str, *, timeout: float = 300, poll_interval: float = 0.5):
        if timeout <= 0 or poll_interval <= 0:
            raise ValueError('timeout and poll_interval must be positive')
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f'Task {task_id} continues in the background')
            try:
                task = self.get_task(task_id, timeout=min(60, remaining))
            except requests.Timeout as exc:
                raise TimeoutError(f'Task {task_id} continues in the background') from exc
            if task['status'] in {'succeeded', 'failed', 'cancelled'}:
                return task
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f'Task {task_id} continues in the background')
            time.sleep(min(poll_interval, remaining))

    def run_now(self, profile_name: str, override: dict[str, Any] | None = None, *, timeout: float = 300):
        task = self.submit_collection(profile_name, override, idempotency_key=uuid4().hex)
        task = self.wait_task(task['task_id'], timeout=timeout)
        if task['status'] != 'succeeded':
            raise RuntimeError(f"Task {task['task_id']} {task['status']}: {task.get('error_text') or ''}")
        return self.get_run(task['run_id'])

    def list_runs(self, limit: int = 50):
        return self.get('/api/runs', limit=limit)

    def get_run(self, run_id: int):
        return self.get(f'/api/runs/{run_id}')

    def search_articles(self, *, query: str = '', country: str = '', category: str = '', duplicates: str = 'any', limit: int = 50):
        return self.get('/api/articles/search', query=query, country=country, category=category, duplicates=duplicates, limit=limit)

    def search_events(self, *, query: str = '', country: str = '', topic: str = '', limit: int = 50):
        return self.get('/api/events/search', query=query, country=country, topic=topic, limit=limit)

    def get_event(self, event_key: str):
        return self.get(f'/api/events/{event_key}')

    def build_editorial(self, event_key: str):
        return self.post('/api/editorial/build', {}, event_key=event_key)

    def build_script(self, event_key: str, profile_name: str = 'douyin_market_60s'):
        return self.post('/api/scripts/build', {'event_key': event_key, 'profile_name': profile_name})

    def get_script(self, package_key: str):
        return self.get(f'/api/scripts/{package_key}')
