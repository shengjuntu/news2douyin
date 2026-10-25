from .openai_client import OpenaiClient
from .client_config import load_config

def create_client_with_config(config_file):
    return OpenaiClient(load_config(config_file))

def create_client(name,options):    
    if name == 'kimi':
        return OpenaiClient(options)
    elif name == 'vllm':
        return OpenaiClient(options)
    else:
        return OpenaiClient(options)