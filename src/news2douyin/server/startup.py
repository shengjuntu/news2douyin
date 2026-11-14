"""Small, process-local startup timings; never collect SQL or configuration values."""
from contextlib import contextmanager
from datetime import datetime, timezone
import logging
import re
from threading import RLock
from time import perf_counter


log = logging.getLogger('uvicorn.error')


class StartupDiagnostics:
    """Record required initialization separately from background graph readiness."""

    def __init__(self, *, clock=None):
        self.clock = clock or perf_counter
        self.started = self.clock()
        self.started_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
        self.lock = RLock()
        self.phases = []
        self.state = 'starting'
        self.finished = None

    @contextmanager
    def phase(self, name):
        # Callers supply code-owned labels, never filenames, URLs or SQL text.
        if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_.]{0,79}', name):
            raise ValueError('Invalid startup phase name')
        started = self.clock()
        row = dict(name=name, state='running', elapsed_ms=0.0, error_type='')
        with self.lock:
            if len(self.phases) >= 64:
                self.phases.pop(0)
            self.phases.append((row, started))
        log.info('startup phase=%s state=running', name)
        try:
            yield
        except BaseException as exc:
            with self.lock:
                row.update(state='failed', error_type=type(exc).__name__)
                self.state = 'failed'
            raise
        else:
            with self.lock:
                row['state'] = 'complete'
        finally:
            with self.lock:
                row['elapsed_ms'] = round(max(0.0, self.clock() - started) * 1000, 3)
                values = dict(row)
            # Exception messages and database/LLM addresses are deliberately omitted.
            log.info('startup phase=%s state=%s elapsed_ms=%.3f error_type=%s',
                     name, values['state'], values['elapsed_ms'], values['error_type'])

    def initialized(self):
        with self.lock:
            if self.state != 'failed':
                self.state = 'initialized'

    def ready(self):
        with self.lock:
            if self.state != 'failed':
                self.state = 'ready'
                self.finished = self.clock()
        log.info('startup state=%s elapsed_ms=%.3f', self.state,
                 self.snapshot(detail=False)['elapsed_ms'])

    def snapshot(self, *, detail=True):
        now = self.clock()
        with self.lock:
            rows = [dict(row, elapsed_ms=round(max(0.0, now - started) * 1000, 3))
                    if row['state'] == 'running' else dict(row) for row, started in self.phases]
            result = dict(scope='current_process', state=self.state,
                          http_ready=self.state == 'ready', started_at=self.started_at,
                          elapsed_ms=round(max(0.0, (now if self.finished is None else self.finished) - self.started) * 1000, 3),
                          graph_readiness_required=False)
        if detail:
            result['phases'] = rows
            result['phase_timing'] = 'inclusive'
        return result
