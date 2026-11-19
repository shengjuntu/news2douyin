"""Factories for the optional chat SDK and standalone embedding adapter."""

__all__ = ['OpenaiClient', 'load_config', 'create_client', 'create_client_with_config']


def __getattr__(name):
    if name == 'OpenaiClient':
        from .openai_client import OpenaiClient
        return OpenaiClient
    if name == 'load_config':
        from .client_config import load_config
        return load_config
    raise AttributeError(name)


def create_client_with_config(config_file):
    from .client_config import load_config
    from .openai_client import OpenaiClient

    return OpenaiClient(load_config(config_file))


def create_client(name, options):
    from .openai_client import OpenaiClient

    return OpenaiClient(options)
