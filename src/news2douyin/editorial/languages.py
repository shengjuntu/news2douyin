"""Output language is independent of the language of the evidence."""
from typing import Literal

OutputLanguage = Literal['zh-CN', 'en', 'ja', 'source']
LANGUAGES = {'zh-CN': '简体中文', 'en': 'English', 'ja': '日本語', 'source': '原文语言'}


def language_instruction(language, duration=60):
    name = LANGUAGES[language]
    return (f'目标口播时长约 {duration} 秒。标题、口播、分镜、备注统一使用 {name}；'
            '输入报道可以是其他语言，请忠实转述事实，保留数字、日期、人名和不确定性。'
            '证据原文和引语快照保持原样，不把译文冒充原文。'
            '原文语言模式沿用首条来源的语言。时长仅指导篇幅，不为凑时长补造事实。')


def analysis_prefix(language):
    return {'en': 'Analysis: ', 'ja': '分析：'}.get(language, '分析：')
