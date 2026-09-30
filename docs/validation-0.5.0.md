# v0.5.0 验证记录

日期：2026-09-30。Linux、Python 3.12、SQLite FTS5 trigram；浏览器 Chrome for Testing 154.0.8037.57。测试全部使用离线自建资料，没有调用真实新闻源、真实 LLM 或 Codex。

## 已通过

| 检查 | 结果 | 范围 |
|---|---:|---|
| `pytest -q -rs` | 124 passed，0 skipped | 包括既有采集/队列/脚本/视频测试及新增 35 项数据与规则回归 |
| 新增数据回归 | 17 项通过 | URL 内容更新、跟踪参数、A→B→A、并发更新、失败回滚、历史稿不变、重试导出当时版本、旧表升级、全库精确副本、FTS 同步、字面检索回退、API/UI |
| 事件归并规则用例 | 18 项通过 | 中英文独立报道、转载、历史补采、不同主体、否定、数字、国家、时间窗、泛化标题等；见 tests/fixtures/event_grouping.json |
| 安装后新闻功能检查 | 14 项通过 | `python -I tools/smoke_news_data.py`；总数与分页、原文不可变、版本历史、来源计数、事件全文、模板资源、携带文章版本的脚本导出 |
| 原安装包冒烟检查 | 18 项通过 | `python -I tools/smoke_installed.py`；采集、任务、脚本审核、包资源、CLI、视频页面兼容 |
| 浏览器交互 | 7 项通过 | 查询表单、翻页保留条件、版本差异、历史切换、多词/独立报道筛选、事件全文与来源数、无 JS 错误 |
| Go Video App 0.5.0 导入 | 4 项通过 | 携带文章版本的来源被接受、正文一致、快照哈希一致、重复导入幂等；未改 Go 源码 |
| 构建与静态检查 | 通过 | wheel + sdist、Python 编译、浏览器脚本语法、git diff --check；sdist 包含 JSON 用例和可选浏览器验证脚本 |

第一次全量测试由于容器没有中文字体跳过了 4 个实际视频用例。随后为验收提供 Noto Sans CJK SC 字体，通过 `NEWS2DOUYIN_VIDEO_FONT` 指定并完整重跑，最终 124 项全部通过。字体仅供验收使用，没有增加发行包运行依赖。浏览器截图也使用中文字体重新检查。

浏览器页面：[文章版本](validation-0.5.0/article-version.png)、[查询与筛选](validation-0.5.0/article-search.png)、[事件来源](validation-0.5.0/event-evidence.png)。结构化结果位于同目录 JSON 文件中。

## 结果的边界

18 组事件用例是手写开发回归样例，并用于调整规则；不独立于开发过程。不能用通过率声称生产归并精确率、召回率或“98% 语义准确率”。待建立真实新闻标注集，分开开发和最终验收样本，分别报告误合并和漏合并。

本次覆盖 SQLite 小库升级和功能一致性，没有做大库迁移、十万篇文章检索性能、长时间并发压力、磁盘满/断电或其他数据库后端验收。无生产用户数据迁移，无 Windows 真机验收。Go 检查仅验证新增来源字段的导入兼容性，不重复声称真实 Codex 端到端或生产新闻审核通过。

## 复现

```bash
pip install -e '.[dev,video,voice-offline]'
# 真实视频用例需要 FFmpeg、FFprobe 和可用中文字体
NEWS2DOUYIN_VIDEO_FONT=/path/to/chinese-font.otf pytest -q -rs
python -I tools/smoke_news_data.py --output /tmp/news-check
```

安装包验收应先安装 wheel，再从仓库外使用 `python -I /path/to/tools/smoke_news_data.py --output /tmp/new-news-check`。每次使用新的输出目录，避免覆盖以前的验收数据库。

可选浏览器检查需要开发环境安装 Playwright，并配置浏览器；产品运行本身不依赖 Node：

```bash
python tools/smoke_news_data.py --output /tmp/news-browser --serve 18190
# 在另一终端执行，或在同一网络命名空间中启动两个进程
BROWSER_BIN=/path/to/chrome node tools/smoke_news_browser.cjs http://127.0.0.1:18190 /tmp/news-browser
python tools/smoke_news_import.py --app /path/to/video-app \
  --script /tmp/news-check/script-package.zip --output /tmp/news-import
```

自动化脚本中的审核确认只用于验证流程。示例资料是虚构的测试内容，不能代替人工事实核对。
