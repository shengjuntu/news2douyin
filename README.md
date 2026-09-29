

# news2douyin 0.1.1 — V7 server-first

This repository now includes a V7 implementation focused on a long-running client/server workflow for market-impression news collection.

Version 0.1.1 fixes event linkage, timezone scheduling, transaction rollback,
search filtering, LLM result validation, configuration and wheel resources.
See [release notes](docs/release-0.1.1.md) for changes, upgrade steps and limits.

Python 3.10+ is required. The default install is the headless service. Optional
features are installed explicitly:

```bash
pip install -e ".[llm]"          # legacy LLM scoring/story/script pipeline
pip install -e ".[assets,tts]"   # legacy asset preparation and voice generation
pip install -e ".[desktop]"      # PyQt clients
pip install -e ".[dev,assets,llm]" # tests and release validation
```

The server loads `.env` from the working directory (or its parents), or the
explicit `NEWS2DOUYIN_ENV_FILE` path. Existing environment variables take
precedence. Never expose an unauthenticated instance to untrusted networks.

## What is new
- Long-running FastAPI server (`news2douyin-server` or `news2douyin v7-serve`)
- Scheduled jobs stored in SQLite
- User-configurable collect profiles (country, language, categories, keywords, source white/black lists)
- Manual backfill / run-now collection
- Stronger de-dup based on canonical URL, normalized title/content hash, and near-duplicate matching
- Persistent article / event storage and search APIs
- Decoupled editorial + video script package generation

## Quick start
```bash
python -m pip install -e .
news2douyin v7-init --profile configs/v7/profiles/mock_market.yaml
news2douyin v7-run-now --profile-name mock_market
news2douyin v7-search-events
news2douyin-server --host 127.0.0.1 --port 18080
```

## Real collection example
```bash
news2douyin v7-init \
  --profile configs/v7/profiles/us_market_macro.yaml \
  --profile configs/v7/profiles/cn_policy_sectors.yaml \
  --jobs-yaml configs/v7/jobs/example_jobs.yaml

news2douyin-server --host 0.0.0.0 --port 18080
```

Main APIs:
- `POST /api/profiles`
- `POST /api/jobs`
- `POST /api/collect/run-now`
- `GET /api/articles/search`
- `GET /api/events/search`
- `POST /api/scripts/build`

# news2douyin

MVP pipeline:
1) Collect daily news (WorldNewsAPI)
2) Filter + deduplicate
3) Score/recommend via LLM (strict JSON)
4) Build storyline packs per event
5) Export Douyin-ready script packs (JSON + Markdown)

V7 currently exports script packages. TTS remains available through the legacy
CLI/GUI with the `tts` extra; V7 does not yet chain TTS or render finished videos.

## Quick start

### Install
```bash
pip install -U pip
pip install -e .
```

### Configure
Create `.env`:
```bash
API_KEY=YOUR_WORLDNEWSAPI_KEY
```

Edit `configs/pipeline.yaml`, install the `llm` extra, and configure
`OPENAI_BASE_URL`, `OPENAI_API_KEY` and `MODEL` for the bundled compatible client.

#### Prompts (file-based)

Built-in prompts ship under `src/news2douyin/prompts/`. They are selected by
`configs/pipeline.yaml`; a local `prompts/` directory overrides the built-ins:

```yaml
prompts:
  dir: prompts
  profile: china_zh
  profiles:
    china_zh:
      scoring: zh/scoring_v1.txt
      story: zh/story_v1.txt
      script: zh/script_v1.txt
    global_en:
      scoring: en/scoring_v1.txt
      story: en/story_v1.txt
      script: en/script_v1.txt
```

By default, script packs are built deterministically (rule-based). To enable LLM script generation:

```yaml
script:
  mode: llm
```

### Run
```bash
python -m news2douyin.cli run --config configs/pipeline.yaml
```

Outputs:
- `runs/YYYY-MM-DD/run_HHMM/`

## LLM integration contract

The pipeline expects:
`create_client_with_config(config_path)` -> client with method:
`generate(prompt: str, max_tokens: int = 900) -> str`

The bundled client implements `generate(prompt, max_tokens=...)`; temperature
is configured in its TOML options. The V7 filter uses the HTTP-compatible client
configuration directly and does not require the `llm` extra.


## PyQt GUI

Install deps and run:

```bash
pip install -e ".[desktop,assets,tts,llm]"
news2douyin-gui
```

Workflow in GUI:
1) **Pipeline** tab: click **Run now** (same as `news2douyin run --config configs/pipeline.yaml`).
2) **Assets + TTS** tab: click **Prep assets** (same as `python tools/prep_assets.py --run-dir ... --out tools/out`).
3) Select an event in **Library + Player**, click **Synthesize** (generates `voice_zh.*` and `voice_en.*`), then **Play**.

Notes:
- TTS uses `edge-tts` if installed (recommended). Fallback is `pyttsx3` (WAV output).
- Schedule: enable daily run in the **Pipeline** tab.
