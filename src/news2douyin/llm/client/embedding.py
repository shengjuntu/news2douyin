from tenacity import retry, wait_random_exponential, stop_after_attempt
from openai import OpenAI

class EmbeddingClient:
    def __init__(self, api_key: str, base_url: str, model: str):
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
        )
        self.model = model

    # 同步方法
    @retry(wait=wait_random_exponential(min=1, max=20), stop=stop_after_attempt(6))
    def get_embedding(self, text) -> list[float]:
        client = self.client
        if isinstance(text, str):
            data = client.embeddings.create(input=[text], model=self.model).data
            return [data[0].embedding]
        else:
            data = client.embeddings.create(input=text, model=self.model).data
            return [x.embedding for x in data]

    # 异步方法
    @retry(wait=wait_random_exponential(min=1, max=20), stop=stop_after_attempt(6))
    async def get_embedding_async(self, text) -> list[float]:
        client = self.client
        if isinstance(text, str):
            data = await client.embeddings.acreate(input=[text], model=self.model)
            return [data.data[0].embedding]
        else:
            data = await client.embeddings.acreate(input=text, model=self.model)
            return [x.embedding for x in data.data]
        
if __name__ == "__main__":
    import numpy as np

    client = EmbeddingClient(api_key="vllm", base_url="http://192.168.0.234:19327/v1", model="bge-large-zh")

    text_1 = "自治区党委党校报告厅"
    text_2 = "香港国际机场"
    vec1 = client.get_embedding(text_1)
    vec2 = client.get_embedding(text_2)

    def cosine_similarity(vec1: np.ndarray, vec2: np.ndarray) -> float:
        """计算两个向量的余弦相似度"""
        return np.dot(vec1, vec2) / (np.linalg.norm(vec1) * np.linalg.norm(vec2))

    # 提取嵌套列表中的向量
    vec1_np = np.array(vec1[0])
    vec2_np = np.array(vec2[0])



    sim = cosine_similarity(vec1_np, vec2_np)
    print(sim)

