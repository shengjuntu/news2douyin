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


## 0.7.0 策略管理验证

- `smoke_management_browser.cjs`：配合 `smoke_daily.py` 的独立离线服务，验证策略试跑、定时、启停与删除；使用 Playwright 和本机 Chromium。
- `smoke_management_installed.py`：安装 wheel 后用 `python -I` 验证真实安装资源和管理流程。
- `seed_upgrade_0_6.py DIRECTORY`：在单独安装 0.6.0 的环境中创建旧数据库样例，包含新闻、任务、选题和脚本。随后安装 0.7.0，运行 `smoke_management_installed.py --upgrade-fixture DIRECTORY` 验证升级。目标目录须不存在。
- 所有演示新闻为离线资料，验证脚本不调用真实新闻源或模型。


## 0.8.0 事件与证据验证

- `smoke_events.py`：独立临时数据库、虚构报道与外部检索响应。
- `smoke_events_browser.cjs`：使用 Playwright/Chromium 完成专题创建、资料加入、节点、检索、合并拆分与策略关键词配置。
- `smoke_events_installed.py`：用 `python -I` 检查安装后的事件 API、页面及证据导出；可传 `--upgrade-fixture DIRECTORY` 验证 0.7.0 数据。
- `seed_upgrade_0_7.py DIRECTORY`：在安装 0.7.0 的单独环境中创建升级样例；目录须事先不存在。

全部使用离线资料，不使用真实新闻或模型账户。


## 0.9.0 有据脚本验证

- `smoke_scripts.py`：临时数据库、虚构资料、已核对节点及模型响应桩；解释模式首次调用故意失败，用于重试验证。
- `smoke_scripts_browser.cjs`：Playwright/Chromium 检查生成选项、关闭页面、逐段编辑、引用拒绝、排序、恢复、审核导出、移动端、取消与重试。
- `smoke_scripts_installed.py`：使用 `python -I` 检查已安装 wheel 的模板、队列、版本和证据导出；可传 `--upgrade-fixture DIRECTORY`。
- `seed_upgrade_0_8.py DIRECTORY`：用安装的 0.8 wheel 创建旧数据与已核对节点，随后安装 0.9 再验证升级。目录必须事先不存在。
- `tests/test_script_generation.py`：冻结输入、两版原文、并发幂等、结构化模型响应、检查点、取消/租约、版本恢复和导出检查。

离线模型响应不证明真实模型的中文写作质量或事实准确性。
