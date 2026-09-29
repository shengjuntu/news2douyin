# news2video prep (selected.json -> raw.jsonl -> scripts + images)

This utility does three things:

1) Map `selected.json` -> corresponding `raw.jsonl` items (and also gather all articles sharing the same `event_id` via `scored.jsonl`).
2) Generate **two separate scripts** per event:
   - `out/scripts/en/<event_id>.md`
   - `out/scripts/zh/<event_id>.md`

   Script generation supports:
   - `--llm openai` (recommended) using OpenAI API
   - `--llm none` (offline fallback; produces a deterministic outline)

3) Download images from original pages (plus any `image_urls` already present in `raw.jsonl`) into:
   - `out/images/<event_id>/<raw_id>/...`

It also writes:
- `out/manifest.json` (events, articles, downloaded images, script paths)

## Quickstart

Offline (no API):
```bash
pip install -r requirements.txt
python prep_assets.py --run-dir ../run_1451 --out out --llm none
```

With OpenAI (better scripts):
```bash
set OPENAI_API_KEY=YOUR_KEY
python prep_assets.py --run-dir ../run_1451 --out out --llm openai --model gpt-4.1-mini
```

## Notes
- Some sites may block scraping; failures are logged and the run continues.
- This tool is designed to be run locally (internet required for image downloads / LLM).
