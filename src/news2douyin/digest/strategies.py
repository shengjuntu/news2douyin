"""Built-in strategies that share the disk-backed GroupedDay evidence index.

Only the built-ins use this registry. Third-party TopN selectors belong at the
candidate-stream boundary and do not have to use this index or its grouping.
"""
from __future__ import annotations

from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .candidate_pool import Candidate, GroupedDay


DEFAULT_STRATEGY = 'balanced'


@dataclass(frozen=True)
class SelectionRequest:
    strategy_id: str
    limit: int
    prior_keys: Collection[str]
    on_merge: Callable[[dict], None] | None


@dataclass(frozen=True)
class Strategy:
    """Version identifies a pinned built-in implementation, independently of the app release."""
    id: str
    version: str
    label: str
    family: str
    run: Callable[[GroupedDay, SelectionRequest], dict]
    requires_baseline: bool = False
    requires_coarse: bool = False


@dataclass(frozen=True)
class PoolRequirements:
    keep_baseline: bool
    coarse: bool


class StrategyRegistry:
    def __init__(self, strategies: Iterable[Strategy]):
        self._by_id = {}
        for strategy in strategies:
            if strategy.id in self._by_id:
                raise ValueError(f'duplicate built-in strategy {strategy.id!r}')
            self._by_id[strategy.id] = strategy

    def all(self) -> tuple[Strategy, ...]:
        return tuple(self._by_id.values())

    def get(self, strategy_id: str) -> Strategy:
        try:
            return self._by_id[strategy_id]
        except KeyError:
            raise ValueError(f'unknown built-in strategy {strategy_id!r}') from None

    def requirements(self, ids: str | Iterable[str]) -> PoolRequirements:
        ids = (ids,) if isinstance(ids, str) else ids
        strategies = [self.get(strategy_id) for strategy_id in ids]
        if not strategies:
            raise ValueError('declare at least one built-in strategy for a prepared day')
        return PoolRequirements(keep_baseline=any(s.requires_baseline for s in strategies),
                                coarse=any(s.requires_coarse for s in strategies))

    def prepare(self, candidates: Iterable[Candidate], ids: str | Iterable[str]):
        """Consume a day once; provision only features required by the requested arms."""
        from .candidate_pool import grouped_candidates

        needed = self.requirements(ids)
        return grouped_candidates(candidates, keep_baseline=needed.keep_baseline, shortlist_trial=needed.coarse)

    def select(self, pool: GroupedDay, strategy_id: str, *, limit: int = 100,
               prior_keys: Collection[str] = (),
               on_merge: Callable[[dict], None] | None = None) -> dict:
        strategy = self.get(strategy_id)
        if type(limit) is not int or limit < 0:
            raise ValueError('limit must be a nonnegative integer')
        if strategy.requires_baseline and not pool.has_legacy_source:
            raise ValueError('Enable keep_baseline to compare the v031 source universe')
        if strategy.requires_coarse and not pool.has_coarse_features:
            raise ValueError('Enable shortlist_trial when grouping the candidates')
        return strategy.run(pool, SelectionRequest(strategy_id, limit, prior_keys, on_merge))


def _ranked(pool, request, *, policy, ranking):
    from .ranked_selection import select_ranked

    return select_ranked(pool, mode=request.strategy_id, policy=policy, ranking=ranking,
                         prior_keys=request.prior_keys, limit=request.limit)


def _shortlist(pool, request, *, variant):
    from .shortlist_trial import select_trial

    return select_trial(pool.db, pool.stats, pool.current_keys(request.prior_keys), variant=variant,
                        limit=request.limit, on_merge=request.on_merge)


def _video(pool, request):
    from .video_selection import rank_video

    return rank_video(pool, prior_keys=request.prior_keys, limit=request.limit)


STRATEGIES = StrategyRegistry((
    Strategy(DEFAULT_STRATEGY, '0.37.0', '现行每日精选', 'production',
             partial(_ranked, policy='current', ranking='balanced')),
    Strategy('balanced_v031', '0.31.0', 'v0.31 平衡排序', 'historical',
             partial(_ranked, policy='v031', ranking='balanced'), requires_baseline=True),
    Strategy('balanced_v032', '0.32.0', 'v0.32 平衡排序', 'historical',
             partial(_ranked, policy='v032', ranking='balanced')),
    Strategy('balanced_v033', '0.33.0', 'v0.33 平衡排序', 'historical',
             partial(_ranked, policy='v033', ranking='balanced')),
    Strategy('breadth', '0.37.0', '报道覆盖排序', 'comparison',
             partial(_ranked, policy='v032', ranking='breadth')),
    Strategy('editorial', '0.37.0', '编辑信号排序', 'comparison',
             partial(_ranked, policy='v032', ranking='editorial')),
    Strategy('video_story', '1.0.0', '视频候选', 'video', _video),
    Strategy('shortlist_classification', '0.37.0', '前置分类实验', 'experimental',
             partial(_shortlist, variant='classification'), requires_coarse=True),
    Strategy('shortlist_merge', '0.37.0', '同行动归并实验', 'experimental',
             partial(_shortlist, variant='merge'), requires_coarse=True),
    Strategy('shortlist_combined', '0.37.0', '分类与同行动归并实验', 'experimental',
             partial(_shortlist, variant='combined'), requires_coarse=True),
))
