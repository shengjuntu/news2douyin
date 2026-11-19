# 新增新闻语种

采集和候选层共用语种标识，例如 `ar`、`es-419` 或 `sr-Latn-RS`。系统统一大小写和分隔符，保留采集时的原始语种字段。缺少可靠语种时，可继续保存候选，页面会把语种标为未知。部分文字系统可以提供明确标注的推测。

## 配置显示名称与信源映射

常见语种的显示名称已内置。如果信源返回标准语种代码，可以直接采集；界面可显示该语种的名称或代码。可用 `NEWS2DOUYIN_LANGUAGE_CONFIG` 指向一个服务进程可读的 TOML 文件，为新语种补显示名称和信源对应关系。修改已配置的文件后，正常请求会读取更新。

```dotenv
NEWS2DOUYIN_LANGUAGE_CONFIG=/data/languages.toml
```

```toml
[labels]
bs = "波斯尼亚语"
"sr-latn-rs" = "塞尔维亚语（拉丁字母，塞尔维亚）"

[providers.gdelt.aliases]
Bosnian = "bs"

[providers.gdelt.query]
bs = "Bosnian"

[providers.worldnewsapi.query]
"sr-latn-rs" = "sr"
```

`aliases` 把上游返回的语种值转换成内部标识。匹配时忽略首尾空白和大小写，原值另行保留。`query` 把策略选择的内部语种转换为实际请求发给上游的代码。上例中的 WorldNewsAPI 映射把细分语种查询放宽为通用的 `sr`，请根据账户和信源支持的实际范围配置。采集器优先使用上游逐条提供的语种字段。

从「运行管理 → 定向采集策略」选择新闻源和语种。GDELT DOC 的选择列表包含配置中的查询映射。WorldNewsAPI 可以在语种输入框填写标识。定向采集的报道可在「候选库」按语种筛选。GKG 继续按完整归档流接收，候选详情按原档可用的元数据显示语种。

语种配置支持来稿的采集、筛选和显示。翻译到哪些语言，以及脚本的成稿语言，由现有翻译和创作设置决定。
