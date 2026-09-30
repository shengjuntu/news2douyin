"""Versioned, frozen layouts and deterministic links from narration to scenes."""
from __future__ import annotations

import copy
import re
from .subtitles import split_narration

TEMPLATES = {
    'brief': dict(id='brief', version=1, name='新闻简报', description='深色标题、来源卡与清晰字幕，适合每日快讯。',
        background='#0b1220', panel='#172a40', ink='#f6f8fc', muted='#a5b4cc', accent='#38bdf8', layout='brief'),
    'explain': dict(id='explain', version=1, name='解释说明', description='浅色阅读版式，突出事实、分析与资料来源。',
        background='#f3f0e9', panel='#e5e8e6', ink='#172b39', muted='#53636c', accent='#137d7a', layout='explain'),
    'timeline': dict(id='timeline', version=1, name='事件复盘', description='节点顺序与时间依据，适合按进展讲述事件。',
        background='#17162a', panel='#29253e', ink='#faf5ed', muted='#c1b4cd', accent='#edae63', layout='timeline'),
}


def catalog():
    return [copy.deepcopy(t) for t in TEMPLATES.values()]


def compact(text):
    return re.sub(r'\s+', '', text)


def scene_plan(snapshot, options):
    doc = snapshot['document']
    parts = doc.get('segments') or [dict(segment_key='whole', text=doc['script_text'], kind='fact',
                                       moment_keys=[], evidence_ids=[], visual=doc.get('visual_notes', ''), assets='')]
    nodes = {m['moment_key']: m for m in snapshot.get('editorial', {}).get('evidence', {}).get('moments', [])}
    sources = {s.get('evidence_id', ''): s for s in snapshot.get('sources', [])}
    if set(options.get('scene_images', {})) - {p['segment_key'] for p in parts}:
        raise ValueError('分段图片的段落已不存在，请重新核对当前脚本')
    scenes = []
    for index, part in enumerate(parts):
        dates = []
        for key in part['moment_keys']:
            node = nodes.get(key)
            if not node:
                continue
            kind = {'occurred': '发生', 'reported': '报道', 'unknown': '时间未知'}[node['time_kind']]
            value = kind if node['time_kind'] == 'unknown' else kind + ' ' + node['date_start']
            if node['date_end']:
                value += ' 至 ' + node['date_end']
            value += {'exact': '', 'approximate': '（大致）', 'disputed': '（有争议）', 'unknown': ''}[node['certainty']]
            dates.append(dict(label=value, note=node['time_note'], title=node['title']))
        selected = set(doc.get('source_keys', []))
        labels = [eid + ' · ' + (sources[eid].get('source_domain') or sources[eid].get('title', ''))
                  for eid in part['evidence_ids'] if eid in sources]
        if not labels:
            labels = [s.get('source_domain') or s.get('title', '') for s in snapshot.get('sources', []) if s['article_key'] in selected]
        assigned = options.get('scene_images', {}).get(part['segment_key'])
        if not assigned and doc.get('segments') and options['image_ids']:
            assigned = options['image_ids'][index % len(options['image_ids'])]
        scenes.append(dict(scene_key=part['segment_key'], index=index + 1, kind=part['kind'],
            narration=('分析：' if part['kind'] == 'analysis' else '') + part['text'],
            title=nodes.get(part['moment_keys'][0], {}).get('title', '') if part['moment_keys'] else '',
            visual=part['visual'], assets=part['assets'], evidence_ids=part['evidence_ids'], source_labels=labels,
            dates=dates, image_id=assigned))
    if compact(''.join(s['narration'] for s in scenes)) != compact(doc['script_text']):
        raise ValueError('分段与审核稿正文不一致，请先修复并重新审核脚本')
    return scenes


def synthesis_chunks(spec):
    if not spec.get('scenes'):
        return split_narration(spec['snapshot']['document']['script_text'])
    return [chunk for scene in spec['scenes'] for chunk in split_narration(scene['narration'])]


def bind_cues(cues, scenes):
    """Exact text offsets, not invented per-word timestamps. Reject boundary-spanning SRT."""
    boundaries, total = [], 0
    for scene in scenes:
        length = len(compact(scene['narration']))
        boundaries.append((total, total + length, scene['scene_key']))
        total += length
    pos, index, result = 0, 0, []
    for number, cue in enumerate(cues, 1):
        length = len(compact(cue['text']))
        while index < len(boundaries) and pos >= boundaries[index][1]:
            index += 1
        if index >= len(boundaries) or pos + length > boundaries[index][1]:
            raise ValueError(f'第 {number} 条字幕跨越脚本段落，请拆成两条并填写各自实际时间')
        result.append(dict(cue, scene_key=boundaries[index][2]))
        pos += length
    if pos != total or compact(''.join(c['text'] for c in cues)) != compact(''.join(s['narration'] for s in scenes)):
        raise ValueError('字幕与固定脚本内容不一致')
    return result


def scene_image(spec, scene, index=0, total=1):
    if scene.get('image_id'):
        return next((img for img in spec['images'] if img['asset_id'] == scene['image_id']), None)
    if spec['snapshot']['document'].get('segments'):
        return None
    # Flat legacy drafts keep the existing sequential image allocation.
    return spec['images'][min(len(spec['images'])-1, index * len(spec['images']) // total)] if spec['images'] else None
