"""Render a movable, offline copy of the blind editorial worksheet."""
import csv
import hashlib
import json
from importlib import resources
from pathlib import Path


def write_review_page(output: Path) -> None:
    worksheet = output / 'review.csv'
    with worksheet.open(encoding='utf-8-sig', newline='') as file:
        rows = list(csv.DictReader(file))
    fields = ('id', 'day', 'title', 'excerpt', 'observed_at', 'source_url',
              'material_origin', 'important', 'fact_group', 'note')
    # Allow only the fields seen by the editor, even if the export later grows.
    blind = [{name: row.get(name) or '' for name in fields} for row in rows]
    data = json.dumps(blind, ensure_ascii=False, separators=(',', ':'))
    storage_key = hashlib.sha256(data.encode()).hexdigest()
    data = data.replace('&', r'\u0026').replace('<', r'\u003c').replace('>', r'\u003e')
    template = resources.files('news2douyin.server').joinpath('templates').joinpath('daily_review_offline.html')
    page = template.read_text(encoding='utf-8').replace('__BUNDLE_ID__', storage_key)
    (output / 'review.html').write_text(page.replace('__BLIND_JSON__', data), encoding='utf-8')
