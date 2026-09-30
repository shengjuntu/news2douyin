

# news2douyin 0.9.0 — 有证据的脚本工作台


延续 0.6.0 的网页主流程：**当日采集 → 勾选新闻 → 生成脚本 → 编辑审核 → 视频制作与导出**。

- `/` 或 `/daily`：每日选题，默认 Asia/Shanghai；已选内容自动保存。
- `/articles`：历史新闻库，支持发布时间、采集时间和首次入库时间筛选。
- `/profiles`：策略增改、复制与启停；可选择热门榜或主动关键词检索；试跑诊断及定时配置。`cn_policy_sectors` 指中国政策与板块，并非 CNN。
- `/events`：新建专题，增删报道，整理绑定文章版本和原文摘录的进展节点，关联背景与后续，合并/拆分，补充检索和证据包导出。
- `/scripts`：逐段脚本、分镜、素材需求与审核；从事件专题的已核对节点后台生成快讯、解释或复盘，可选时长和风格。支持无需模型的证据提纲和 AI 有据初稿。
- `/videos`：视频任务和成片；已有视频功能的依赖要求保持不变。
- `/admin`：原仪表盘、自动采集任务和运行管理。
- `/timeline`：原时间线已明确为“新闻长图排版”。

本版将已核对的发展脉络与固定来源版本接入脚本生成。每段区分事实和分析，绑定节点与来源，可编辑分镜、素材需求。生成在后台执行，关闭页面后可继续查看、取消或重试；新稿不会覆盖已有稿件。引用结构校验不能代替人工核实。

首次使用可启动服务后在“采集策略”新增一个 `mock` 演示策略，回到首页采集并验证完整流程。
真实新闻选择 `worldnewsapi`，在 `.env` 填入 `API_KEY`；AI 脚本配置 `OPENAI_BASE_URL`、`OPENAI_API_KEY`、`MODEL`。
WorldNewsAPI 现在提供两种获取方式：**热门榜单**和**主动关键词检索**，随后执行策略筛选。专题中的补充检索先保存候选，用户选择后才收录。覆盖范围受新闻源和账户权限影响。

[本版说明与升级步骤](docs/release-0.9.0.md) · [模块关系与后续版本计划](docs/product-roadmap.md) · [验证记录](docs/validation-0.9.0.md)

This repository now includes a V7 implementation focused on a long-running client/server workflow for market-impression news collection.

Version 0.5.0 adds immutable article versions, independent-report event grouping, and SQLite FTS5 search with pagination. See [release notes and upgrade steps](docs/release-0.5.0.md).

Version 0.4.1 adds a handoff guide and paired demo bundles for RunDesk Video App 0.3.0. See [cross-app handoff](docs/release-0.4.1.md).

Version 0.4.0 adds a local video pipeline: approved script → speech or uploaded
PCM WAV + SRT → captioned portrait frames → H.264/AAC MP4. Video tasks use the
existing queue, cancellation and checkpoints. See [video setup and limits](docs/release-0.4.0.md).
The [versioned script workbench from 0.3.0](docs/release-0.3.0.md) is included.
[Persistent background tasks from 0.2.0](docs/release-0.2.0.md) are included.
The [0.1.1 correctness and packaging fixes](docs/release-0.1.1.md) are included.

Python 3.10+ is required. The default install is the headless service. Optional
features are installed explicitly:

```bash
pip install -e ".[llm]"          # legacy LLM scoring/story/script pipeline
pip install -e ".[assets,tts]"   # legacy asset preparation and voice generation
pip install -e ".[video,voice-offline]" # local video + offline preview speech
pip install -e ".[desktop]"      # PyQt clients
pip install -e ".[dev,assets,llm,video,voice-offline]" # tests and release validation
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
- `POST /api/profiles` / `DELETE /api/profiles/{name}`
- `POST /api/profiles/{name}/enable` / `disable` / `test`
- `GET /api/tasks/{task_id}/diagnostics`
- `POST /api/jobs/preview` / `PUT|DELETE /api/jobs/{id}`
- `POST /api/jobs`
- `POST /api/tasks/collect` (202 + task ID)
- `GET /api/tasks/{task_id}` / `events` / `stream`
- `POST /api/tasks/{task_id}/cancel` / `retry`
- `POST /api/collect/run-now` (legacy wait; `?wait=false` queues)
- `GET /api/articles/search`
- `GET /api/events/search`
- `POST /api/events/{event_key}/script-tasks` / `GET /api/script-tasks/{task_id}`
- `GET /api/script-tasks?event_key=...`
- `POST /api/scripts/build`
- `GET /api/scripts` / `GET|PUT /api/scripts/{package_key}`
- `GET /api/scripts/{package_key}/revisions`
- `POST /api/scripts/{package_key}/review` / `exports`
- `GET /api/video/capabilities`
- `POST /api/video/assets` / `GET /api/video/assets?package_key=...`
- `POST /api/video/tasks` / `GET /api/video/tasks/{task_id}`

# news2douyin

MVP pipeline:
1) Collect daily news (WorldNewsAPI)
2) Filter + deduplicate
3) Score/recommend via LLM (strict JSON)
4) Build storyline packs per event
5) Export Douyin-ready script packs (JSON + Markdown)

Open `/scripts` for the script workbench, or generate a draft from an event page.
V7 can render approved scripts as portrait MP4 videos. Install the `video` extra,
FFmpeg/FFprobe (with libx264 and AAC), and a CJK font. Use `voice-offline` for
mechanical preview speech, `tts` for Edge TTS (network required), or upload PCM
WAV plus a matching SRT. Open the script's “制作视频” page.
[Offline generated example](examples/video-demo.mp4) · [Reproduce it](tools/smoke_video.py).

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
