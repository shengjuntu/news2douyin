# news2douyin 0.13.3：研究搜索 HTTP 400

此页为 0.13.3 的历史修复记录。0.14.0 已移除研究模块的直接搜索路径，当前设计及升级请看 [研究检索职责](RESEARCH-SEARCH.md)。

日期：2026-10-13。基于 0.13.2，继续配套 RunDesk 0.48.0。

## 已确认的问题与边界

旧提示“搜索服务返回 HTTP 400”由 research/sources.py 的直接搜索适配器生成，表示当前研究使用 WorldNewsAPI 或 Tavily。RunDesk MCP / Exa 路径的 research_search 只发检索票据，不在 news2douyin 中调用 Exa。

源代码允许 1000 字符检索词，但 WorldNewsAPI 文档规定 3–100 字符；旧代码未在发请求前执行这一限制。默认课题截止时间可包含小数秒，旧代码将它原样传给 WorldNewsAPI 日期字段，没有按文档示例输出整秒时间。旧错误只保留 HTTP 状态，无法判断某次请求究竟违反哪个限制。

这两个适配问题已经修复，但没有取得用户那次请求的上游错误正文，也未使用真实搜索账号复现，所以不将其表述为该次 400 的唯一原因。

## 修改

- WorldNewsAPI 检索词去除首尾空白后要求 3–100 字符；无效请求在预留预算前拒绝，提示改为较短关键词，不静默截断。Exa 和本地库仍保留各自原有长度规则。
- 截止时间解析为 UTC，WorldNewsAPI 使用 YYYY-MM-DD HH:mm:ss，Tavily 使用 YYYY-MM-DD。课题内的原始截止时间与证据日期校验不变。
- 错误显示 WorldNewsAPI / Tavily 名称、HTTP 状态和有长度限制的上游 JSON 错误；发送的 API Key 及常见认证字段脱敏，不输出原始请求头、整个响应或 HTML 错误页。
- 相同失败请求仍不自动重发，避免重复付费；返回时明确显示“本次未重新请求”。
- 研究页面显示当前及历史每轮实际使用的检索服务，数据来自启动时保存的环境快照。
- MCP serverInfo 版本与软件包同步为 0.13.3。

## 升级与恢复

1. 在新闻研究页停止仍在执行的旧研究；确认停止完成。
2. 停止 news2douyin 服务，在原 Python 环境从解压目录安装：

   ```bash
   python -m pip install --upgrade ./release-0.13.3/news2douyin-0.13.3-py3-none-any.whl
   ```

3. 用原启动命令和原数据目录重启服务，刷新浏览器。无需重建 RunDesk 的 Codex Docker 镜像；若 news2douyin 服务本身采用容器部署，则更新该服务容器的安装包。
4. 如要使用已经配置成功的 Exa，在 `/research/settings` 选择“RunDesk MCP”和“Exa”，点击“连接并配置环境”。只改变“搜索后端”但仍选 WorldNewsAPI / Tavily，不会切换直接搜索路径。
5. 在原课题点击“启动环境并继续研究”，创建新一轮，保留原有证据与报告。研究页应显示“RunDesk MCP / exa”。旧轮次按启动快照运行，修改设置不会追溯改变它。
6. 如果继续使用直接搜索，失败信息现在会显示具体提供方和可用的上游错误。若同一轮已有缓存失败，完成修正后应开始新一轮；不通过重复原请求判断修复是否生效。

## 验证

针对搜索 HTTP 请求新增 11 项用例，与原研究回归合计 71 项通过。覆盖 UTC/小数秒日期、WorldNewsAPI 长度上下界及预算不扣减、Exa 长查询票据、两种提供方错误详情与密钥脱敏、非 JSON 错误、失败缓存和冻结搜索配置。前端 JavaScript 通过 Node 语法检查。

网络请求使用测试替身；没有连接用户真实 Docker、Codex、WorldNewsAPI、Tavily 或 Exa 账号，没有据此宣称真实新闻研究已验收。

参数依据（核对于 2026-10-13）：

- [WorldNewsAPI Search News](https://worldnewsapi.com/docs/search-news/)
- [Tavily Search](https://docs.tavily.com/documentation/api-reference/endpoint/search)
