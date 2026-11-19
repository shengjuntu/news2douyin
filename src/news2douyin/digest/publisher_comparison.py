"""Replay two daily policies on the same local observations and compare their lists."""
from collections import deque
from datetime import timedelta
from importlib import resources
import json
from urllib.parse import urlsplit

from jinja2 import Environment

from .calendar import days_before
from .publisher_families import MAP_VERSION
from .strategies import STRATEGIES


def _day_changes(previous, current, pool):
    old = {card['key']: (rank, card) for rank, card in enumerate(previous['items'], 1)}
    new = {card['key']: (rank, card) for rank, card in enumerate(current['items'], 1)}
    common = old.keys() & new.keys()
    # Both policies retain the same progress identifier in this release.
    # Include the current count for old-list entries which dropped out as well.
    materials = pool.current_cards(old.keys() | new.keys())
    changes = []
    same_position = 0
    for key in sorted(old.keys() | new.keys()):
        old_rank, old_card = old.get(key, (None, {}))
        new_rank, new_card = new.get(key, (None, {}))
        card = materials[key]
        breadth = card['publisher_breadth']
        if old_rank == new_rank:
            same_position += 1
            if not breadth.get('folded'):
                continue
        if old_rank is None:
            status = 'entered'
        elif new_rank is None:
            status = 'left'
        elif old_rank == new_rank:
            status = 'same'
        else:
            status = 'moved'
        changes.append(dict(key=key, status=status, old_rank=old_rank, new_rank=new_rank,
                            old_score=old_card.get('score'), new_score=new_card.get('score'),
                            title=card['title'], url=card['source_url'],
                            raw_hosts=card['domain_count'], raw_hosts_capped=card['domains_capped'],
                            publisher_breadth=breadth))
    def display_order(item):
        order = ('entered', 'left', 'moved', 'same').index(item['status'])
        rank = item['new_rank'] if item['new_rank'] is not None else item['old_rank']
        return order, rank
    return dict(old_selected=len(old), new_selected=len(new), common=len(common),
                same_position=same_position, entered=len(new)-len(common), left=len(old)-len(common),
                old_prior_excluded=previous['stats']['prior_excluded'],
                new_prior_excluded=current['stats']['prior_excluded'],
                changes=sorted(changes, key=display_order))


def compare_publishers(start, end, candidates_for_day, *, limit=100, warmup=7):
    if end < start or type(limit) is not int or limit < 1 or not 0 <= warmup <= 7:
        raise ValueError('请选择连续日期、正整数名额、0 至 7 天预热。')
    first = days_before(start, warmup)
    modes = ('balanced_v033', 'balanced')
    histories = {mode: deque(maxlen=7) for mode in modes}
    report = dict(older='0.33.0', newer='0.34.0', edition='replay', limit=limit,
                  warmup_days=(start-first).days, mapping_version=MAP_VERSION, days={})
    for offset in range((end-first).days+1):
        day = first + timedelta(days=offset)
        counts, editions = {}, {}
        with STRATEGIES.prepare(candidates_for_day(day, counts), modes) as pool:
            for mode in modes:
                prior = {key for previous_day in histories[mode] for key in previous_day}
                issue = STRATEGIES.select(pool, mode, limit=limit, prior_keys=prior)
                editions[mode] = issue
                histories[mode].append([card['key'] for card in issue['items']])
            if day >= start:
                result = _day_changes(editions[modes[0]], editions[modes[1]], pool)
                result['source_counts'] = counts
                report['days'][day.isoformat()] = result
    return report


def count_text(count, capped):
    return f'{count}+' if capped else str(count)


def format_comparison(report):
    lines = [f"本地资料回放 · v{report['older']} 与 v{report['newer']} · 各自预热 {report['warmup_days']} 天"]
    for date, day in report['days'].items():
        lines.append(f"{date}　旧榜 {day['old_selected']} 条，新榜 {day['new_selected']} 条，"
                     f"两榜共有 {day['common']} 条，其中 {day['same_position']} 条名次相同。")
        if day['source_counts'].get('gkg_archive_present') is False:
            lines.append('  未找到本地 GKG 归档，本日仅比较事务库中可回放的资料。')
        for item in day['changes']:
            if item['status'] not in ('entered', 'left'):
                continue
            pos = f"入榜第 {item['new_rank']}" if item['status']=='entered' else f"离榜，原第 {item['old_rank']}"
            breadth = item['publisher_breadth']
            hosts = count_text(item['raw_hosts'], item['raw_hosts_capped'])
            units = count_text(breadth['units'], breadth['units_capped'])
            lines.append(f"  {pos}　{item['title']}　（域名 {hosts}，排序计数 {units}）")
    return '\n'.join(lines)


def format_comparison_html(report):
    def web_address(url):
        try:
            parsed = urlsplit(url)
            if parsed.scheme in ('https', 'http') and parsed.hostname and not parsed.username and not parsed.password:
                return url
        except ValueError:
            pass
        return ''

    template = resources.files('news2douyin.server').joinpath('templates').joinpath('publisher_comparison.html')
    env = Environment(autoescape=True)
    env.filters['web_address'] = web_address
    env.filters['count_text'] = count_text
    return env.from_string(template.read_text(encoding='utf-8')).render(report=report)


def write_comparison(report, out=None):
    if out is None:
        print(format_comparison(report))
        return
    suffix = out.suffix.lower()
    if suffix == '.html':
        body = format_comparison_html(report)
    elif suffix == '.json':
        body = json.dumps(report, ensure_ascii=False, indent=2)
    elif suffix == '.txt':
        body = format_comparison(report)
    else:
        raise ValueError('对照报告支持 .html、.json 和 .txt。')
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('x', encoding='utf-8') as file:
        try:
            file.write(body + '\n')
        except OSError:
            file.close()
            out.unlink(missing_ok=True)
            raise
    print(f'已保存对照报告 {out}')
