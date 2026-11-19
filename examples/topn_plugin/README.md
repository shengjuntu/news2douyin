# 示例策略：按报道最近观测时间选 Top N

`latest-diverse` 从当天每条符合输入条件的候选观测出发，给每个原文 URL 保留最新版本，再按观测时间选前 N 篇。每个来源主机最多入选 `max_per_domain` 篇。同一个标题出现在两家媒体，可以保留两篇。这是一种适合检查最近更新、控制单一来源占比的选法。结果可以少于 N。

## 安装与切换

在运行日报后台的同一个 Python 环境中，安装 news2douyin 0.38 和这个独立包。在主项目目录执行：

```sh
python -m pip install ./examples/topn_plugin
export NEWS2DOUYIN_SELECTION_CONFIG="$PWD/examples/topn_plugin/selection.toml"
news2douyin-daily-radar rebuild --day 2026-11-15
news2douyin-daily-radar export --day 2026-11-15 --out shortlist.md
```

示例配置选择 3 篇，每个来源主机最多 2 篇。调整 `[selection].limit` 和 `[selection.options].max_per_domain` 后重新生成目标日，即可比较结果。Web 后台从其服务进程的环境读取相同的配置路径。首次给服务设置环境变量时重启服务，再在“日报管理”申请重建。再次导出已发布日报会保留当版的条目和顺序。

## 写一个自己的算法

1. 复制这个目录，修改 `pyproject.toml` 中的分发包名、入口点名称和类路径。入口点分组保持为 `news2douyin.selection`。
2. 类声明 `api_version = 1`、算法版本 `version`，在构造函数中读取和校验该策略的 `options`。如果需要上游读取 GKG 粗分类元数据，可声明 `needs_coarse = True`。
3. 实现 `select(candidates, context)`。候选迭代器支持单遍扫描，同一标题的多条独立观测都可参与。上下文包含北京时间自然日、带时区的排他截止时间、正整数 `limit` 和历史已选键 `prior_keys`。历史补采按正式日报的数据可见性规则处理。
4. 按推荐顺序产出不超过 `context.limit` 个 `SelectionPick`。每条使用入流的 `candidate` 作为代表，提供稳定唯一的 `key`、有限数值 `score` 和简短 `reasons`。算法自行确定去重单位。示例使用原文 URL 的散列，并以候选自身的键处理时间并列，保证结果可复现。
5. 安装新包，把配置中的 `strategy` 改为入口点名称，写入相应的 `[selection.options]`。

从环境中移除示例配置并清除显式策略覆盖后，新生成的日报使用内置默认策略。已发布的旧日报仍可查看和导出。

## 验收

使用已安装主包、示例包和主项目 `dev` 测试依赖的环境，从主项目目录运行：

```sh
python -I examples/topn_plugin/acceptance_check.py
```

脚本在临时 SQLite/DuckDB 库中建立 123 条当日可见 GKG 观测和一条恰好到达截止线的观测，实际执行 Web 后台重建、CLI 重建和 Markdown 导出。它核对 N=3 的三域名结果、N=110 的全量选择、N=200 时的 122 篇可用报道、同 URL 更新和不带原文链接的历史日报，并切回默认算法复核原名单。
