from __future__ import annotations

from typing import Any

import requests


class Client:
    def __init__(self, base_url: str = 'http://127.0.0.1:18080') -> None:
        self.base_url = base_url.rstrip('/')

    def get(self, path: str, **params):
        resp = requests.get(self.base_url + path, params=params, timeout=60)
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

    def run_now(self, profile_name: str, override: dict[str, Any] | None = None):
        return self.post('/api/collect/run-now', {'profile_name': profile_name, 'override': override or {}})

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
