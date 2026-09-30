from __future__ import annotations

import re
import unicodedata


def split_narration(text, max_units=64):
    """Bounded caption/synthesis chunks, retaining all non-whitespace text."""
    chunks, current, units = [], '', 0
    for char in text:
        size = 2 if unicodedata.east_asian_width(char) in {'W', 'F'} else 1
        if current and units + size > max_units:
            chunks.append(current.strip())
            current, units = '', 0
        current += char
        units += size
        if char in '。！？!?；;\n':
            if current.strip():
                chunks.append(current.strip())
            current, units = '', 0
    if current.strip():
        chunks.append(current.strip())
    return chunks


def timecode(seconds):
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f'{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}'


def write_srt(path, cues):
    path.write_text('\n\n'.join(f"{i}\n{timecode(c['start'])} --> {timecode(c['end'])}\n{c['text']}"
                               for i, c in enumerate(cues, 1)) + '\n', encoding='utf-8')


def parse_srt(text, script_text):
    cues = []
    pattern = r'(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})'
    def seconds(value):
        match = re.fullmatch(pattern, value.strip())
        if not match:
            raise ValueError('SRT 时间格式应为 HH:MM:SS,mmm')
        h, m, s, ms = map(int, match.groups())
        if m > 59 or s > 59:
            raise ValueError('SRT 时间无效')
        return h * 3600 + m * 60 + s + ms / 1000
    for block in re.split(r'\n\s*\n', text.replace('\r\n', '\n').strip()):
        lines = block.strip().splitlines()
        if lines and lines[0].strip().isdigit():
            lines.pop(0)
        if len(lines) < 2 or '-->' not in lines[0]:
            raise ValueError('SRT 缺少时间轴或正文')
        start, end = map(seconds, lines[0].split('-->', 1))
        body = '\n'.join(lines[1:]).strip()
        if start < 0 or end <= start or (cues and start < cues[-1]['end']) or not body or len(body) > 240:
            raise ValueError('SRT 不允许时间重叠、空正文或超过 240 字符的单条字幕')
        cues.append({'start': start, 'end': end, 'text': body})
    compact = lambda s: re.sub(r'\s+', '', s)
    if not cues or len(cues) > 300 or compact(''.join(c['text'] for c in cues)) != compact(script_text):
        raise ValueError('SRT 正文必须与审核稿完全一致（忽略空白）且不超过 300 条')
    return cues
