# 0.23.0 验证记录

日期：2026-11-12。基线：已发布的0.22.0源码包。本轮实现PostgreSQL在线事务层，保留DuckDB原档和FalkorDB图层；没有访问或修改用户现场27GB数据库。

## 实际结果

- 通用回归：445 passed、29 skipped、15 warnings，105.62秒。未设置PG测试URL时，PG专项明确跳过；警告为已有依赖弃用提示。后续新增的PG和队列专项另在下面的联合检查执行，不能将两组数量相加作为唯一用例总数。
- 联合专项：49 passed，38.62秒。真实PostgreSQL 16.2，涵盖产品集成10项、搜索/触发器6项、迁移13项、后台三层链路1项、SQLite/PG队列隔离16项、连接/WAL配置3项。
- PostgreSQL产品集成验证文章版本不可变、重复启动、并发采集/导入/投影、断点重放、雷达/专题/内容工作台、JSON及时间查询；23个GET只发查询，不消费后台待办或同步修复旧绑定。
- 持有文章逻辑锁和未提交修改期间，GET仍可读取旧版本，其他文章能入库；相同文章写冲突返回503和Retry-After，提交或回滚后锁正常释放。
- PostgreSQL搜索使用真实pg_trgm扩展，EXPLAIN确认命中GIN索引；缺扩展或权限失败时使用savepoint隔离错误，明确报告扫描降级，字面中文/百分号和日期偏移语义保持一致。临时PG二进制原不带pg_trgm，QA中从官方PostgreSQL 16.2对应源码构建了扩展，不包含在产品包。
- 迁移验证只读源、停源确认、非空WAL拒绝、Unicode复合主键续传、同事务记录断点、已提交批次故障恢复、源变化与目标篡改拒绝、逐表全行哈希、Boolean/JSON原字节、版本/ID与AUTOINCREMENT高水位、目标已有对象拒绝、连接秘密脱敏、报告不能覆盖源。旧0.22源无GraphLease时新目标该表为空。
- 安装后验证发现clean WAL只读打开时会产生空日志记账文件，已修复其源指纹误判；新增空日志身份与真实WAL模式源迁移回归，迁移定向最终15项全部通过（7.15秒），确认源主库字节不变、非空日志/实际变化仍拒绝，半迁移目标拒绝启动，恢复ready后正常启动。这组是在联合49项之后追加和复测的迁移检查。
- 实际后台接收/投影/抽取/索引/图线程，连接真PG、DuckDB 1.4.5和FalkorDB：38条原始记录全部保留，36投影、2跳过，35文章/版本、34事件。投影期间四个核心页面多轮全部200，游标与offset完整收敛，真实图存在EventMention及专题Question→HAS_MATERIAL→Event。按线程监测，网络、DuckDB、抽取和GRAPH调用不持有PG事务。上游HTTP使用受控ZIP夹具。
- Chromium＋真实PG/FalkorDB浏览器验证雷达交互图→选题→研究→初稿→作品、专题与采集，移动端无横向溢出，无页面JS错误；研究正文为受控夹具。浏览器截图仅供内部检查，不包含在发布包。
- Python编译、JS语法和Compose结构/端口/环境配置检查通过。安装包与资源验证另见release-0.23.0/VALIDATION.txt。

## 复现入口

```bash
python -m pip install '.[dev,research,llm,assets]'
pytest -q
```

真实PG检查可在另一个QA环境安装pgserver==0.1.4和psycopg[binary]，用该环境的Python执行：

```bash
python tools/run_postgres_test_server.py -- /path/to/product-venv/bin/python -m pytest -q tests/test_postgres_integration.py tests/test_postgres_search_triggers.py tests/test_postgres_migration.py tests/test_postgres_pipeline.py tests/test_worker_isolation.py tests/test_storage_backends.py
```

包装器使用临时集群和随机数据库，监听本机回环地址，运行完销毁，不用于生产。也可自行提供专用QA管理员连接NEWS2DOUYIN_TEST_POSTGRES_URL；测试仅创建/删除自己命名的随机数据库或schema。

真实图检查另需FalkorDB测试二进制。本轮为保持产品redis<8依赖，用QA环境分别安装falkordblite==0.11.0和falkordb==1.4.0（--no-deps）及psutil，仅用其redislite内附的服务二进制，产品图连接仍使用redis客户端；不建议将此QA组合用于生产。部署使用独立FalkorDB服务。

## 范围与限制

没有构建Docker镜像或运行完整Compose，提供的正式Compose配置为PG17；本轮实际数据库版本是PG16.2。没有在CentOS7或Windows实机运行本轮服务，也未验证27GB迁移耗时、长期公网GKG吞吐、磁盘增长、真实LLM/RunDesk/搜索质量。

GKG全量是从所选起点开始全部批次行的保存，不表示新闻全文或全部历史已下载。迁移保留原有大快照结构，没有自动删正文、搬出全部历史正文或清理原档；这些需要独立容量审计与可回退迁移。DuckDB仍由单应用进程管理，PG并发能力不等于可以启动多个进程共写同一DuckDB目录。
