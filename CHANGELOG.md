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

