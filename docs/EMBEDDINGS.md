# 接入 BGE-M3 向量服务

在 news2douyin 的实际运行目录中编辑 `.env`，将地址替换为应用能访问的 vLLM 地址。也可以用 `NEWS2DOUYIN_ENV_FILE` 指定配置文件。进程环境中的同名变量优先。

```dotenv
EMBEDDING_BASE_URL=http://embedding-host:11435/v1
EMBEDDING_MODEL=
EMBEDDING_DIMENSIONS=1024
EMBEDDING_API_KEY=
```

模型名留空时，客户端从 `GET /v1/models` 中自动选择唯一的模型 ID。如果服务公布多个 ID，填写其中一个。只有接口公布 `bge-m3` 这个 ID 时才使用 `EMBEDDING_MODEL=bge-m3`。服务需要认证时填写 `EMBEDDING_API_KEY`。1024 用于校验返回的向量维度。[vLLM 0.30 模型名设置](https://docs.vllm.ai/en/v0.30.0/cli/serve/#-served-model-name)

## 验收与生效

从应用所在网段、使用部署环境的 Python 执行同一个检查。它先读取模型列表，再批量请求一组中英文测试句并校验输出维度。

```bash
python -m news2douyin.llm.client.embedding --check
```

应用按 `.env` 读取向量配置。修改后按现有部署方式重启，Docker Compose 部署需重新创建应用容器以应用环境变量。

向量客户端随 news2douyin 提供，使用现有的 `requests` 依赖。接入验收调用模型列表和 OpenAI 兼容向量接口。[vLLM 0.30 Embeddings API](https://docs.vllm.ai/en/v0.30.0/models/pooling_models/embed/)

## 事件向量

配置 `EMBEDDING_BASE_URL` 后，系统在整理事件提交时登记后台任务。向量内容包括标题、摘要、已核对的时间线描述和有原文依据的行动摘录。内容哈希不变时复用已有向量。可用 `NEWS2DOUYIN_EVENT_EMBEDDING_ENABLED=0` 暂停编码。

在「运行管理 → 知识处理状态 → 事件向量」按日期提交历史补算。日期按系统时区解释，使用事件的首次记录时间。历史计划、事件任务和重试进度都保存在事务库中，重启后续跑。找不到模型、接口错误或无效向量会自动尝试重试，最终失败的事件可以按原日期再次补算。

同页可以输入描述检索相似事件。它按日期和同一套模型配置过滤，最多在最近的 5,000 条合格向量中计算余弦相似度。结果链接到已整理事件。第一次启用时可选近期范围补算，验证可搜后再扩大日期。更换向量模型、地址、维度或同名权重时，按需要重新补算相应日期。权重在模型 ID 不变时可以通过增加 `NEWS2DOUYIN_EVENT_EMBEDDING_MODEL_REVISION` 区分。

向量保存在 PostgreSQL 事务库。部署已经启用 `vector` 扩展时，检索优先使用 pgvector 计算余弦距离。普通 PostgreSQL 和 SQLite 也支持有界的相似度计算。正式 Compose 可选设 `NEWS2DOUYIN_POSTGRES_IMAGE=pgvector/pgvector:pg17-bookworm`。换镜像时保留同主版本和原数据卷，并在目标数据库执行 `CREATE EXTENSION IF NOT EXISTS vector;`。[pgvector 安装说明](https://github.com/pgvector/pgvector#docker)

## 具体进展的共享向量

世界态势按去重的行动编号生成进展向量，独立于上述已整理事件索引。日图与程序溯源共用进展向量。历史补算、覆盖状态和模型版本说明见[每日语义地图](SEMANTIC-MAP.md)。
