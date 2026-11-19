"""Compatibility entry points for the daily radar candidate pool and named rankings."""
from collections.abc import Collection, Iterable

from .candidate_pool import Candidate, GroupedDay, MAX_DOMAIN_SAMPLE, MAX_SOURCES, grouped_candidates
from .headline_identity import card_progress_key, progress_keys
from .ranked_selection import BALANCED_MODES, MODES, check_legacy_limit, selection_reasons as _reasons


def rank_day(candidates: Iterable[Candidate], *, limit: int = 100,
             prior_keys: Collection[str] = (), mode: str = 'balanced') -> dict:
    """Run an existing named ranking on one day's observation stream."""
    from .strategies import STRATEGIES

    check_legacy_limit(limit)
    if mode not in MODES:
        raise ValueError(f'unknown digest mode {mode!r}')
    with STRATEGIES.prepare(candidates, (mode,)) as pool:
        return STRATEGIES.select(pool, mode, limit=limit, prior_keys=prior_keys)
