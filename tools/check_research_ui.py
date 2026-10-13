"""Render the actual templates and exercise their JS handlers in a DOM stub.

Requires Node. This checks interaction contracts, not browser layout.
"""
import json
from pathlib import Path
import subprocess
from jinja2 import Environment, FileSystemLoader, select_autoescape

root = Path(__file__).resolve().parents[1]
templates = Environment(loader=FileSystemLoader(root/'src/news2douyin/server/templates'), autoescape=select_autoescape(['html']))
pages = {name: templates.get_template(name+'.html').render(active='research', article_key='seed', event_key='', case_id='case-test')
         for name in ['research_settings', 'research', 'research_case']}
data = {'pages': pages, 'common': (root/'src/news2douyin/server/static/research.js').read_text(),
        'caseJS': (root/'src/news2douyin/server/static/research-case.js').read_text()}
subprocess.run(['node', str(root/'tools/research-ui-contract.cjs')], input=json.dumps(data), text=True, check=True)
