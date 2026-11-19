# 每日 Top N 策略配置与插件接口

每日精选把当天的采集候选交给一套可配置的策略。策略决定怎样去重、聚合、打分和排序，返回最多 N 条。正式日报生成、历史重算和 Web 后台用同一个入口。Markdown 导出读取已发布版，可以继续交给 Codex 选出做视频的十条。

## 切换策略

未配置时使用内置 `balanced`，上限 100 条。把以下内容保存为 `daily-selection.toml`，用服务进程可见的绝对路径设置 `NEWS2DOUYIN_SELECTION_CONFIG`。每次生成只读取一次配置文件。修改文件后，在「日报管理」重建目标日期，或运行 `news2douyin-daily-radar rebuild --day YYYY-MM-DD`。

```toml
[selection]
strategy = "balanced"
limit = 100
```

| 字段 | 用途 |
| --- | --- |
| `strategy` | 已安装的入口点名称或内置算法名 |
| `limit` | 正整数，包括大于 100 的配置。满足策略规则的候选较少时，日报可以少于 N 条 |
| `options` | 该算法自己的参数，格式为 `[selection.options]`。内置策略接受空参数 |

运行 `news2douyin-review strategies` 列出可配置的策略。已有粗分类、同行动归并和组合实验也有独立名称。发布的实验对照命令仍使用其指定的默认基线。

每期保存策略名、版本、N、接口版本和参数。页面可以看到实际生效的策略，非默认配置生成的完整 Markdown 也带策略和版本。参数属于可审计数据，模型密钥等凭据由插件从环境读取。配置无效、插件缺失、执行出错或返回不合格结果时，本次重建失败。已有版次仍可查看和导出。

首次设置环境变量后，需要重启服务。用正式 Compose 时，把 TOML 放到宿主机持久数据目录中，并在 `.env` 设置容器内路径，例如 `NEWS2DOUYIN_SELECTION_CONFIG=/data/daily-selection.toml`。若使用独立插件，请安装到同一个应用镜像。可创建自己的派生 Dockerfile，在当前应用镜像上复制并安装插件 wheel。通过 CLI 生成或导出时，须使用对应数据库和同一个持久数据目录。

## 接入独立插件

源码中的 [latest-diverse 示例](../examples/topn_plugin/README.md) 提供一个独立 Python 包。它按报道 URL 合并更新、每个域名限额，再按最近观测时间选取。解压源码包后可以直接安装。

```bash
python -m pip install ./examples/topn_plugin
export NEWS2DOUYIN_SELECTION_CONFIG="$PWD/examples/topn_plugin/selection.toml"
news2douyin-review strategies
news2douyin-daily-radar rebuild --day 2026-11-15
news2douyin-daily-radar export --day 2026-11-15 --out shortlist.md
```

独立发行包在自己的 `pyproject.toml` 注册入口点。

```toml
[project.entry-points."news2douyin.selection"]
latest-diverse = "news2douyin_latest_docs:LatestDocuments"
```

构造器接收 `options` 字典并校验它支持的参数。策略对象声明 `api_version = 1` 和独立的算法 `version`，实现以下接口。需要上游提供 GKG 粗分类字段时，声明 `needs_coarse = True`。

```python
from collections.abc import Iterable

from news2douyin.digest.selection import Candidate, SelectionContext, SelectionPick

def select(
    self, candidates: Iterable[Candidate], context: SelectionContext
) -> Iterable[SelectionPick]:
    ...
```

- `candidates` 是截至出刊时间可见的单遍候选流，包含 GDELT GKG 的可读记录、官方事件及其可读报道、本地事件和 NewsNow。没有标题或链接的记录继续保存在原档。官方事件、本地事件和 NewsNow 按投影对象给出一个可读代表，GKG 按归档行进入。选择模块把这批全天候选交给策略。独立插件可以自行使用标题、实体、信号、外部图或语义模型来分组。
- `context.day` 是北京时间自然日，`cutoff` 为带时区的排他截止时间，`limit` 是 N，`prior_keys` 为近日已发布条目标识。历史补采适用该日期的出刊规则。程序化入口 `select_top_n(..., day=..., cutoff=..., n=..., strategy=..., options=...)` 使用调用方提供的当天候选。
- 输出 `SelectionPick(candidate=..., key=..., score=..., reasons=..., related=...)`，按推荐顺序生成。每项使用当轮输入的代表和关联来源。策略可以暂存完整候选对象，取回后保持原字段。自定义条目标识须稳定、唯一，长度 1–160 个可打印字符。分数须是有限数值，框架保留插件给出的顺序。没有指定 `key` 时采用代表的候选标识。
- 插件可逐批或用本地临时存储处理候选。页面区分来源提供的条数与插件实际读取条数。插件随应用在同一 Python 进程运行，安装来源由运维管理。

## 验证记录

- 八个日期的 GKG 回放覆盖 25,559 条可读输入。默认名单、顺序和 Markdown 与 v0.37 一致。另用实际 Events、Mentions、GKG 混合输入验证示例策略选出 150 条。[真实数据回放](validation/0.38.0/real-replay.json)
- 在独立 Python 环境安装主包和示例包，验证按配置切换、截止边界、上限大于 100、Web 后台重算和 CLI 导出。[插件安装验收](validation/0.38.0/plugin-install.json)
- 使用 SQLite 和 PostgreSQL 跑同一套合成回放，验证默认卡片、Markdown 和四组对照输出。[跨数据库回放](validation/0.38.0/synthetic-backend-parity.json)
