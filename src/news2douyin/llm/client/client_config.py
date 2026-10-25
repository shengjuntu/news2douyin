import toml

def load_config(file_path):
    try:
        with open(file_path, 'r') as f:
            config = toml.load(f)
            openai_config = config.get('openai_llm', {})
            # 解析 OpenAI LLM 的配置参数并放入字典中
            params = {
                'do_sample':openai_config.get('do_sample',False),
                'seed':openai_config.get('seed',0),
                'temperature': openai_config.get('temperature', 0.7),
                'max_tokens': openai_config.get('max_tokens', 50),
                'top_p': openai_config.get('top_p', 0.9),
                'presence_penalty': openai_config.get('presence_penalty', 0.0),
                'frequency_penalty': openai_config.get('frequency_penalty', 0.0),
                'best_of': openai_config.get('best_of', 1),
                'timeout':openai_config.get('timeout',10),
                'retry_count':openai_config.get("retry_count", 3),
                'retry_delay':openai_config.get("retry_delay", 10),
                'verbose':openai_config.get("verbose", False)
            }
            return params
    except FileNotFoundError:
        print("文件不存在:", file_path)
        return None

