import requests
import json
from loguru import logger

class EmbeddingClient:
    def __init__(self, url):
        self.url = url

    def create(self, query:str, task:str=""):

        payload = {
            "query": query,
            "task": task,  
        }
        try:
            response = requests.post(self.url, headers={"Content-Type": "application/json"}, data=json.dumps(payload))
            if response.status_code == 200:
               return response.json()['embedding']
            else:
                logger.error("error: {}", response.text)
                return None
        except Exception as e:
            logger.error("An exception occurred: {}", e)
            return None


import numpy as np

def test_fn(client):
    
    # Each query must come with a one-sentence instruction that describes the task
    task = 'Given a web search query, retrieve relevant passages that answer the query'
    queries = [
        task + 'how much protein should a female eat',
        task + 'summit define'
    ]
    # No need to add instruction for retrieval documents
    documents = [
        "As a general guideline, the CDC's average requirement of protein for women ages 19 to 70 is 46 grams per day. But, as you can see from this chart, you'll need to increase that if you're expecting or training for a marathon. Check out the chart below to see how much protein you should be eating each day.",
        "Definition of summit for English Language Learners. : 1  the highest point of a mountain : the top of a mountain. : 2  the highest level. : 3  a meeting or series of meetings between the leaders of two or more governments."
    ]
    embeddings = [1,2,3,4]

    embeddings[0] = client.create(queries[0])
    embeddings[1] = client.create(queries[1])
    embeddings[2] = client.create( documents[0])
    embeddings[3] = client.create(documents[1])

    scores = (np.array(embeddings[:2]) @ np.array(embeddings[2:]).T)*100
    #scores = (embeddings[:2] @ embeddings[2:].T) * 100
    print(scores.tolist())
    
   
    

# 使用示例
if __name__ == "__main__":
    import torch
    # 假设服务器运行在本地 8000 端口
    client = EmbeddingClient("http://192.168.0.234:8880/embedding/")
    
    # 定义关系定义和查询
    test_fn(client)
    
    
