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

验收通过后，这份配置可供后续的事件向量检索使用。本版接入入口是客户端和独立验收命令。后续应用进程需要重新加载 `.env` 时，按现有部署方式重启服务。使用 Docker Compose 时，重新创建应用容器以应用环境变量。

向量客户端随 news2douyin 提供，使用现有的 `requests` 依赖。接入验收调用模型列表和 OpenAI 兼容向量接口。[vLLM 0.30 Embeddings API](https://docs.vllm.ai/en/v0.30.0/models/pooling_models/embed/)
