# 在本机下载 GDELT 历史归档

在国际线路较好的电脑上运行下载器。电脑访问 GDELT 官方站和 news2douyin 的 HTTP 服务，服务器将接收的文件按现有规则校验，发布为 Parquet。事件溯源通过 DuckDB 读取已发布分区。本设置适用于「历史归档」，日常实时采集沿用其已有配置。

## 服务器

升级后在服务实际读取的环境中设置以下两项，并重启服务。第一项默认值为 `direct`。令牌可用 `python -c "import secrets; print(secrets.token_urlsafe(32))"` 生成，再分别写入两端。

```dotenv
NEWS2DOUYIN_HISTORY_ARCHIVE_SOURCE=relay
NEWS2DOUYIN_HISTORY_RELAY_TOKEN=<同一个随机令牌>
```

进入「运行管理 → 历史归档」，选择日期，点击「开始补档」。默认取英文 Events 和全字段 GKG，可勾选英文 Mentions。已有任务可在升级后接着处理。归档页的「本地下载器」里可以下载单文件脚本。

## 下载电脑

需要 Python 3.10 及以上，只用标准库。将脚本保存为 `gdelt_history_relay.py`。下面以现有内网地址为例，外网连接请使用 HTTPS。令牌与服务端一致，命令中的提示文字须换成实际值。

Windows PowerShell：

```powershell
$env:NEWS2DOUYIN_HISTORY_RELAY_TOKEN = '替换为服务器上的令牌'
py -3 gdelt_history_relay.py --server http://192.168.0.234:3180 --cache-dir .\gdelt-cache
```

Linux 或 macOS：

```bash
export NEWS2DOUYIN_HISTORY_RELAY_TOKEN='替换为服务器上的令牌'
python3 gdelt_history_relay.py --server http://192.168.0.234:3180 --cache-dir ./gdelt-cache
```

下载器会持续领取任务。先按需传官方清单，服务器据此安排缺少的 ZIP，下载器再校验并上传。服务器确认接收后，下载器清理已传的 ZIP。上传中断时，重启并沿用相同的缓存目录即可。参数 `--once` 只处理一次领取的工作，用于连通性测试；首轮可能只上传清单。安装过本版完整软件的电脑也可用 `news2douyin-gdelt-relay` 运行相同命令。

## 查看结果

在归档页可查看下载器上次联系时间，以及「服务器已接收」和「已可查询」两项进度。服务器收到并校验 ZIP 后计入第一项，同组文件完成处理并发布到 Parquet 后计入第二项。开始时两项可能不同。先选一个过去的 UTC 日即可检验传输、转换、查询的整个过程。接着可以从事件溯源节点按需提交缺档范围。

遇到 401 时检查两端令牌，遇到 503 时先检查服务端下载模式和令牌配置。通过反向代理访问时，代理需允许流式 PUT 和足够大的请求体，首次传输的官方总清单可能比单个 ZIP 大得多。可查分区及已有覆盖记录都保存在原历史归档中，切回服务器直连时把来源改为 `direct` 并重启。
