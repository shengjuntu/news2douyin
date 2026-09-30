"""Bounded local media operations. No shell commands or user-supplied URLs."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as src:
        for block in iter(lambda: src.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def binary(name):
    value = os.environ.get('NEWS2DOUYIN_' + name.upper(), name)
    path = shutil.which(value)
    if not path:
        raise RuntimeError(f'找不到 {name}；请安装并加入 PATH，或配置 NEWS2DOUYIN_{name.upper()}')
    return path


def run_process(args, *, cwd, check=lambda: None, timeout=180, progress=None):
    """Drain output to a file; cancellation also kills the running subprocess."""
    check()
    started = time.monotonic()
    log = Path(cwd) / ('process-' + str(time.time_ns()) + '.log')
    with log.open('wb') as output:
        proc = subprocess.Popen([str(a) for a in args], cwd=cwd, stdout=output, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=os.name != 'nt')
        try:
            while proc.poll() is None:
                check()
                if time.monotonic() - started > timeout:
                    raise TimeoutError('媒体处理超时')
                if progress:
                    progress()
                time.sleep(0.15)
            check()
            if proc.returncode:
                with log.open('rb') as src:
                    src.seek(max(0, log.stat().st_size - 2500))
                    message = src.read().decode('utf-8', errors='replace')
                raise RuntimeError(f'媒体处理失败 ({proc.returncode}): {message}')
        finally:
            if proc.poll() is None:
                if os.name != 'nt':
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                else:
                    proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    if os.name != 'nt':
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:
                        proc.kill()
                    proc.wait(timeout=3)
    return log


def probe(path, *, check=lambda: None):
    path = Path(path).resolve()
    check()
    result = subprocess.run([binary('ffprobe'), '-v', 'error', '-protocol_whitelist', 'file,pipe',
        '-show_format', '-show_streams', '-of', 'json', str(path)], capture_output=True, timeout=20)
    check()
    if result.returncode:
        raise ValueError('无法识别媒体文件')
    return json.loads(result.stdout)


def font_path():
    configured = os.environ.get('NEWS2DOUYIN_VIDEO_FONT')
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise RuntimeError('NEWS2DOUYIN_VIDEO_FONT 指定的字体不存在')
        return path.resolve()
    for path in [Path('C:/Windows/Fonts/msyh.ttc'), Path('/System/Library/Fonts/PingFang.ttc'),
                 Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')]:
        if path.is_file():
            return path
    if shutil.which('fc-list'):
        result = subprocess.run(['fc-list', ':lang=zh', '-f', '%{file}\n'], capture_output=True, timeout=10)
        for entry in result.stdout.decode().splitlines():
            candidate = Path(entry.strip())
            if candidate.is_file():
                return candidate
    if shutil.which('fc-match'):
        result = subprocess.run(['fc-match', '-f', '%{file}', 'sans:lang=zh'], capture_output=True, timeout=10)
        candidate = Path(result.stdout.decode().strip())
        if candidate.is_file():
            return candidate
    raise RuntimeError('找不到中文字体，请配置 NEWS2DOUYIN_VIDEO_FONT')


def capabilities():
    result = {'ffmpeg': False, 'ffprobe': False, 'pillow': importlib.util.find_spec('PIL') is not None,
              'edge': importlib.util.find_spec('edge_tts') is not None,
              'espeak': importlib.util.find_spec('espeakng_loader') is not None,
              'font': False, 'errors': []}
    for name in ['ffmpeg', 'ffprobe']:
        try:
            executable = binary(name)
            args = [executable, '-hide_banner', '-encoders'] if name == 'ffmpeg' else [executable, '-version']
            checked = subprocess.run(args, capture_output=True, timeout=8)
            if checked.returncode:
                raise RuntimeError(f'{name} 无法运行，请检查安装与执行权限')
            result[name] = True
            if name == 'ffmpeg':
                entries = {line.split()[1] for line in checked.stdout.decode(errors='replace').splitlines() if len(line.split()) > 1}
                result['encoders'] = {codec: codec in entries for codec in ['libx264', 'aac']}
                if not all(result['encoders'].values()):
                    result['errors'].append('FFmpeg 缺少 libx264 或 AAC 编码器，请安装包含这两种编码器的版本')
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            result['errors'].append(str(exc))
    try:
        selected = font_path()
        result['font_name'] = selected.name
        result['font'] = True
        if result['pillow']:
            from PIL import ImageFont
            font = ImageFont.truetype(str(selected), 32)
            if bytes(font.getmask('中')) == bytes(font.getmask('\uffff')):
                result['font'] = False
                result['errors'].append('字体缺少中文字符，请设置 NEWS2DOUYIN_VIDEO_FONT')
    except (RuntimeError, OSError) as exc:
        result['font'] = False
        result['errors'].append(str(exc))
    if not result['pillow']:
        result['errors'].append('请安装 news2douyin[video]')
    result['ready'] = all(result[k] for k in ('ffmpeg', 'ffprobe', 'pillow', 'font')) and all(result.get('encoders', {}).get(k, False) for k in ['libx264','aac'])
    result['checks'] = [dict(key=key, label=label, ok=bool(ok), fix=fix) for key,label,ok,fix in [
        ('ffmpeg','FFmpeg 可执行程序',result['ffmpeg'],'安装 FFmpeg 并加入 PATH，或设置 NEWS2DOUYIN_FFMPEG。'),
        ('encoders','H.264 / AAC 编码器',all(result.get('encoders', {}).get(k, False) for k in ['libx264','aac']),'FFmpeg 构建需包含 libx264 和 AAC。'),
        ('ffprobe','媒体检查程序',result['ffprobe'],'安装 FFprobe 并加入 PATH，或设置 NEWS2DOUYIN_FFPROBE。'),
        ('pillow','图片处理依赖',result['pillow'],'在运行服务的 Python 环境执行 pip install "news2douyin[video]"。'),
        ('font','中文字体',result['font'],'安装中文字体，或将 NEWS2DOUYIN_VIDEO_FONT 指向本地字体文件。'),
        ('espeak','离线机械配音（可选）',result['espeak'],'执行 pip install "news2douyin[voice-offline]"，或选上传配音。'),
        ('edge','Edge TTS（可选，未验证联网）',result['edge'],'执行 pip install "news2douyin[tts]"，或选上传配音。')]]
    return result
