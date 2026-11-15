# 0.28.0：直接接入NewsNow中文热点

直接读取用户现有NewsNow服务的`/api/s?id=`JSON数据。增加NewsNow采集策略选项、来源勾选、逐源试跑诊断和配套配置，沿用现有正式采集/定时任务，不新增主菜单。

接入以用户上传的现场方案为参考，在当前0.27.0上实现。八种时间格式统一处理，缺少发布时间保持未知；标题和摘要标为待补原文线索。全源失败明确报错，成功空榜与失败分开，在线条数采用跨源轮转；股票榜不冒充新闻。

每轮保留来源快照和状态，原始响应内容寻址去重，不把整榜重复写入文章版本。新Article只保存小型追溯元数据。任务的大raw/filtered阶段保存校验文件指针，减少PostgreSQL重复大载荷。无需更换数据库或清空历史；PostgreSQL＋DuckDB＋FalkorDB和GDELT六流保持。

NewsNow摘要不能直接作为核实正文证据，仅有线索时先补材/深研究；原文随后到达可升级同一Article并进入正常事件链。已有完整报道不会被短摘要覆盖。仅榜单位置、快照报告时间等变化不会增加文章版本。

## 升级与使用

停止服务并按原部署流程备份配置、数据库与完整storage_root，保留数据库地址、目录和图命名空间。用实际服务Python安装：

```bash
python -m pip install --upgrade "./release-0.28.0/news2douyin-0.28.0-py3-none-any.whl[research]"
python -c "import importlib.metadata,news2douyin;print(importlib.metadata.version('news2douyin'));print(news2douyin.__file__)"
```

Editable部署完整更新源码和pyproject后重新`pip install -e .`。本版不新增业务表、不自动删除数据或压缩旧湖。

进入运行管理的定向采集策略，选择NewsNow，服务地址预填`http://192.168.0.234:4444`，勾来源后先试跑，再正式采集并按需建立定时计划。配套YAML在`configs/v7/profiles/newsnow_cn.yaml`，详细契约与部署机只读探测命令见[NewsNow说明](NEWSNOW.md)。

## 验收边界

本地真实HTTP服务复现了用户样本与错误形态，并验证SQLite/实际PostgreSQL、TaskWorker、浏览器和原采集回归。开发环境不能访问用户私网，未声称35源已再次现场测通，也未自动部署。实际条数随榜单变化，按部署机探测和试跑结果验收。

本版完成数据接入与快照留存；尚未实现跨平台热点归并、GDELT事件自动关联或观众热度综合排序。验证结果见[验证记录](validation/0.28.0/README.md)。
