# 0.16.0

- 首页升级为选题工作台：跨日期搜索历史报道与事件、固定原文、持久化编辑与选择，不绑定某一天。
- 新增 RunDesk 库内选题会话：一句话开始、候选卡片、带原文引用的回复、连续追问复用会话；采用建议需显式操作。
- 新增 5 个 topic_* MCP 工具，总工具数 13；保存检索快照与原文阅读记录，支持日期、分页、事件关联报道。
- 从选题交接仅用资料写短稿、轻量补材或深入研究；素材报告模式不强制生成脚本。原文版本、参数和交接编号固定，断线/重试沿用原任务。
- 新增6张选题表，旧每日选择、研究、脚本和素材继续读取；复用原执行器、环境准备、回执与停止机制。
- 详见 docs/TOPICS.md 与 docs/validation/0.16.0。真实模型、GPU、Exa 和 Docker 部署需环境联调。

# 0.15.1

- 新增顶部“补充材料”导航；补材任务页提供明显的发起按钮。
- 脚本工作台、每日选题和深入研究页新增补材入口，直达已展开的补材模式。
- `/daily?mode=short#script-builder` 预选补材，筛选、分页和刷新保留模式；快捷切换保留当日选题与筛选条件，仍需明确点击启动。
- 补材搜索、RunDesk 调用、预算、稿件保存及既有任务 API 保持兼容。

# 0.15.0

- Add explicit Chinese/English/Japanese output language and short duration options to daily and evidence-based script generation; preserve source-language extraction without pretending to translate.
- Add a bounded short-material pass using the existing RunDesk/Codex/Exa runtime: up to six selected articles, three external searches and six new source reads, with a five-minute material budget.
- Add `research_save_short_script`, which validates claim ownership and fact/analysis citations and atomically saves an evidence-bound editable draft plus material report.
- Discover image candidates from actual source pages; retrieve up to four bounded public JPEG/PNG/WebP files, show previews/provenance, and include images and an unverified-rights manifest in reviewed exports. Image failures retain text results.
- Preserve idempotency, cancellation fences and immutable evidence. New language/duration requests retain previous drafts. Add the additive ShortScriptBrief table; RunDesk 0.48.0 and its Docker image remain compatible.
- See docs/SHORT-SCRIPTS.md and docs/validation/0.15.0 for upgrade and validation scope. No live model/search or actual container verification is claimed.

# 0.14.0

- Remove direct WorldNewsAPI/Tavily adapters from agent research. Codex / Exa is the sole external research search route; collection and event material gathering remain unchanged.
- Keep the seven MCP tool names. `research_search(web)` reserves a ticket only; `library` searches stored articles without consuming external search budget.
- Deduplicate default/explicit web scope and surrounding query whitespace, reuse recorded outcomes and existing Exa tickets, and serialize concurrent reservations.
- Migrate legacy search settings, preserve credentials/history, and require a fresh run for legacy routes instead of returning cached HTTP 400 errors.
- Check Exa discovery before submission in Docker and local mode; explain HTTP 400 without assuming quota exhaustion. Simplify settings to a single search owner.
- Verify 72 research tests, 4 collection/event search tests and 8 two-service HTTP/MCP checks; Docker, Codex and external search remain fixtures.

# 0.13.3

- Validate WorldNewsAPI search keywords (3–100 characters) before reserving research search budget; preserve longer Exa ticket queries.
- Normalize direct search cutoff timestamps to UTC and serialize WorldNewsAPI timestamps to whole seconds; keep Tavily date-only fields.
- Identify the actual search provider and retain bounded, redacted JSON error details; explicitly label repeated cached failures without automatic retries.
- Display each run's frozen search provider and explain how to switch new runs to RunDesk MCP / Exa.
- Align MCP server version with the package. Research regressions: 71 passed; live paid search and model execution not tested.

# 0.12.0

## 0.13.0

- Add one-click preparation of the application's bound Docker environment before research; require RunDesk 0.47.0.
- Install missing business MCP/Skill in the container during initial setup; preserve administrator edits and deleted capabilities after initialization.
- Check binding, authentication metadata and tool discovery before submitting a model turn; retain durable receipt recovery and guarded stop.
- Reuse active research on duplicate start and persist per-run environment/search configuration.
- Record Exa/Wigolo search tickets and returned leads without treating snippets as original evidence.
- Release the settings lock during network setup to allow business MCP callbacks.
- Preserve local execution for existing settings. Document first-time image/auth/network setup and unverified live-deployment boundaries.


- 应用主动注册并复用旧实例；连接不覆盖后台配置。
- Skills / MCP / 模型集中在 RunDesk 管理；应用只提交业务任务。
- 会话查看入口与默认协作配置简化。
- 详见 docs/APPLICATION-CONNECT.md。

