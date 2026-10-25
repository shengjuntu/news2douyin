"""Local H.264 video pipeline with checkpointed sentence-level speech."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import time
import wave
from uuid import uuid4
import zipfile

from sqlmodel import Session

from ..storage.models import VideoProduction
from .media import binary, file_hash, probe, run_process
from .production import PRESETS
from .subtitles import split_narration, write_srt
from .workbench import canonical, digest
from .templates import synthesis_chunks, bind_cues, scene_image

SAMPLE_RATE = 24000


def artifact(path):
    path = Path(path).resolve()
    return {'path': str(path), 'sha256': file_hash(path), 'size_bytes': path.stat().st_size}


def valid(entry):
    path = Path(entry.get('path', ''))
    return path.is_file() and path.stat().st_size == entry.get('size_bytes') and file_hash(path) == entry.get('sha256')


def wav_duration(path):
    with wave.open(str(path)) as audio:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getframerate() != SAMPLE_RATE:
            raise ValueError('配音检查点的格式不正确')
        return audio.getnframes() / SAMPLE_RATE


def normalize_audio(source, output, context):
    run_process([binary('ffmpeg'), '-v', 'error', '-nostdin', '-y', '-protocol_whitelist', 'file,pipe',
                 '-i', str(source), '-map', '0:a:0', '-ac', '1', '-ar', str(SAMPLE_RATE),
                 '-t', '601', '-c:a', 'pcm_s16le', str(output)], cwd=output.parent, check=context.check)
    duration = wav_duration(output)
    if not 0.1 <= duration <= 600:
        raise ValueError('配音长度必须在 0.1–600 秒之间')
    return duration


def make_audio(spec, folder, context):
    cached = context.checkpoint('video_audio')
    if cached and valid(cached['audio']):
        return cached
    options = spec['options']
    script = spec['snapshot']['document']['script_text']
    output = folder / 'narration.wav'
    if options['backend'] == 'uploaded':
        audio = spec['audio']
        if file_hash(audio['path']) != audio['sha256']:
            raise ValueError('上传配音校验失败')
        duration = normalize_audio(audio['path'], output, context)
        cues = spec['cues']
        if cues[-1]['end'] > duration + 0.03:
            raise ValueError('字幕超出配音时长')
        alignment = 'user_supplied_srt'
    else:
        chunks = synthesis_chunks(spec)
        if not 1 <= len(chunks) <= 300:
            raise ValueError('分句数量需在 1–300 之间')
        cues, duration, segments = [], 0.0, []
        context.progress('voice', 0, len(chunks))
        for index, text in enumerate(chunks):
            context.check()
            entry = context.checkpoint(f'video_voice_{index}')
            if not entry or not valid(entry['audio']):
                text_path = folder / f'text-{index:04}.txt'
                text_path.write_text(text, encoding='utf-8')
                raw = folder / (f'voice-{index:04}' + ('.mp3' if options['backend'] == 'edge' else '.wav'))
                run_process([sys.executable, '-m', 'news2douyin.video.voice', '--backend', options['backend'],
                             '--input', str(text_path), '--output', str(raw), '--voice', options['voice'],
                             '--rate', str(options['rate'])], cwd=folder, check=context.check)
                converted = folder / f'audio-{index:04}.wav'
                normalize_audio(raw, converted, context)
                entry = {'audio': artifact(converted)}
                context.save_checkpoint(f'video_voice_{index}', entry, replace=True)
            path = Path(entry['audio']['path'])
            length = wav_duration(path)
            cues.append({'start': duration, 'end': duration + length, 'text': text})
            duration += length
            if duration > 600:
                raise ValueError('配音总时长超过 600 秒，请拆分脚本')
            segments.append(path)
            context.progress('voice', index + 1, len(chunks))
        with wave.open(str(output), 'wb') as merged:
            merged.setparams((1, 2, SAMPLE_RATE, 0, 'NONE', 'not compressed'))
            for path in segments:
                context.check()
                with wave.open(str(path)) as segment:
                    merged.writeframes(segment.readframes(segment.getnframes()))
        duration = wav_duration(output)
        alignment = 'synthesized_sentence_durations'
    if spec.get('scenes'):
        cues = bind_cues(cues, spec['scenes'])
    result = {'audio': artifact(output), 'duration': duration, 'cues': cues, 'alignment': alignment}
    context.save_checkpoint('video_audio', result, replace=True)
    return result


def wrap_text(text, font, width):
    lines, current = [], ''
    for char in text:
        if char == '\n':
            lines.append(current)
            current = ''
        elif current and font.getlength(current + char) > width:
            lines.append(current)
            current = char
        else:
            current += char
    if current:
        lines.append(current)
    return lines


def draw_card(path, spec, size, cue, image_path=None):
    if spec.get('template'):
        from .visuals import draw_template
        return draw_template(path, spec, size, cue, image_path)
    from PIL import Image, ImageDraw, ImageFont, ImageOps
    width, height = size
    canvas = Image.new('RGB', size, '#0b1220')
    draw = ImageDraw.Draw(canvas)
    for y in range(height):
        draw.line((0, y, width, y), fill=(11 + y * 4 // height, 18 + y * 10 // height, 32 + y * 16 // height))
    font = spec['font']['path']
    title_font = ImageFont.truetype(font, round(width * 0.067))
    label_font = ImageFont.truetype(font, round(width * 0.026))
    title = spec['snapshot']['document']['title']
    # Reject a font with missing Chinese glyphs instead of producing tofu boxes.
    missing = bytes(title_font.getmask('\uffff'))
    for char in set(title + cue['text']):
        if '\u4e00' <= char <= '\u9fff' and bytes(title_font.getmask(char)) == missing:
            raise ValueError('所配置字体缺少中文字符，请安装中文字体或配置 NEWS2DOUYIN_VIDEO_FONT')
    margin = round(width * 0.07)
    draw.rounded_rectangle((margin, height * .045, margin + width * .12, height * .05), radius=2, fill='#38bdf8')
    draw.text((margin, height * .065), 'NEWS BRIEF', font=label_font, fill='#93c5fd')
    title_lines = wrap_text(title, title_font, width - 2 * margin)
    if len(title_lines) > 3:
        title_lines = title_lines[:3]
        title_lines[-1] = title_lines[-1][:-1] + '…'
    for i, line in enumerate(title_lines):
        draw.text((margin, height * .10 + i * title_font.size * 1.3), line, font=title_font, fill='white')
    box = (margin, round(height * .30), width - margin, round(height * .67))
    if image_path:
        with Image.open(image_path) as img:
            img = ImageOps.fit(img.convert('RGB'), (box[2] - box[0], box[3] - box[1]))
            canvas.paste(img, box[:2])
    else:
        draw.rounded_rectangle(box, radius=round(width * .025), fill='#15283e', outline='#284663', width=2)
        center_font = ImageFont.truetype(font, round(width * .075))
        draw.text((width / 2, height * .45), '新闻简报', font=center_font, fill='#cbd5e1', anchor='mm')
        selected = set(spec['snapshot']['document']['source_keys'])
        sources = [s for s in spec['snapshot']['sources'] if s['article_key'] in selected]
        date = sources[0].get('published_at', '')[:10] if sources else ''
        draw.text((width / 2, height * .52), date, font=label_font, fill='#93c5fd', anchor='mm')
    caption_font = None
    for font_size in range(round(width * .055), round(width * .030) - 1, -1):
        caption_font = ImageFont.truetype(font, font_size)
        lines = wrap_text(cue['text'], caption_font, width - 2 * margin)
        orphan = len(lines) > 1 and any(line and all(c in '。，、；：！？,.!?;:' for c in line) for line in lines)
        if len(lines) * font_size * 1.5 <= height * .23 and not orphan:
            break
    else:
        raise ValueError('单条字幕过长，请缩短 SRT 中的字幕分段')
    y = height * .71
    for line in lines:
        draw.text((margin, y), line, font=caption_font, fill='#f8fafc')
        y += caption_font.size * 1.5
    draw.text((margin, height * .965), 'NEWS2DOUYIN', font=label_font, fill='#64748b')
    canvas.save(path)


def make_frames(spec, audio, folder, context):
    width, height, fps = PRESETS[spec['options']['preset']]
    cues, timeline, position = audio['cues'], [], 0.0
    for cue in cues:
        if cue['start'] > position + .0005:
            timeline.append({'start': position, 'end': cue['start'], 'text': '', 'scene_key': timeline[-1].get('scene_key') if timeline else cue.get('scene_key')})
        timeline.append(cue)
        position = cue['end']
    if audio['duration'] > position + .0005:
        timeline.append({'start': position, 'end': audio['duration'], 'text': '', 'scene_key': cues[-1].get('scene_key')})
    images = spec['images']
    for entry in images:
        if file_hash(entry['path']) != entry['sha256']:
            raise ValueError('图片素材校验失败')
    lines, scene_frames = [], []
    context.progress('frames', 0, len(timeline))
    for index, cue in enumerate(timeline):
        context.check()
        path = folder / f'frame-{index:04}.png'
        image = images[min(len(images)-1, index * len(images) // len(timeline))]['path'] if images else None
        if spec.get('scenes'):
            scene = next(s for s in spec['scenes'] if s['scene_key'] == cue['scene_key'])
            selected_image = scene_image(spec, scene, index, len(timeline))
            image = selected_image['path'] if selected_image else None
            scene_frames.append(dict(start=cue['start'], end=cue['end'], scene_key=scene['scene_key'],
                kind=scene['kind'], evidence_ids=scene['evidence_ids'], dates=scene['dates'],
                image_id=selected_image['asset_id'] if selected_image else None, text=cue['text']))
        draw_card(path, spec, (width, height), cue, image)
        lines.extend([f"file '{path.name}'", f"duration {cue['end'] - cue['start']:.6f}"])
        context.progress('frames', index + 1, len(timeline))
    lines.append(f"file 'frame-{len(timeline)-1:04}.png'")
    (folder / 'frames.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    shutil.copyfile(folder / 'frame-0000.png', folder / 'cover.png')
    if spec.get('scenes'):
        (folder / 'scene_timeline.json').write_text(canonical({'schema_version': 1, 'template': spec['template'], 'scenes': spec['scenes'], 'frames': scene_frames}), encoding='utf-8')
    return width, height, fps


def encode_video(folder, duration, fps, context):
    context.progress('rendering', 0, 100)
    last_value, last_time = -1, 0.0
    def progress():
        nonlocal last_value, last_time
        if time.monotonic() - last_time < 0.7:
            return
        last_time = time.monotonic()
        path = folder / 'ffmpeg-progress.txt'
        if path.exists():
            values = [line.split('=', 1)[1] for line in path.read_text(errors='replace').splitlines() if line.startswith('out_time_us=')]
            if values and values[-1].isdigit():
                value = min(99, int(int(values[-1]) / 1000000 / duration * 100))
                if value != last_value:
                    context.progress('rendering', value, 100)
                    last_value = value
    run_process([binary('ffmpeg'), '-v', 'error', '-nostdin', '-y', '-filter_threads', '1',
        '-f', 'concat', '-safe', '1', '-i', 'frames.txt', '-i', 'narration.wav',
        '-map', '0:v:0', '-map', '1:a:0', '-vf', f'fps={fps},tpad=stop_mode=clone:stop_duration=600', '-c:v', 'libx264', '-threads', '2', '-preset', 'fast',
        '-crf', '23', '-pix_fmt', 'yuv420p', '-r', str(fps), '-vsync', 'cfr',
        '-c:a', 'aac', '-b:a', '128k', '-t', f'{duration:.6f}', '-movflags', '+faststart',
        '-progress', 'ffmpeg-progress.txt', 'video.mp4'], cwd=folder, check=context.check,
        timeout=min(3600, max(180, duration * 20)), progress=progress)
    context.progress('rendering', 100, 100)


def run_video(engine, task, storage_root, context):
    with Session(engine) as session:
        production = session.get(VideoProduction, task.task_id)
        if production is None:
            raise ValueError('缺少视频任务输入')
        spec = json.loads(production.spec_json)
        input_hash, revision, export_key = production.input_hash, production.revision, production.export_key
    if digest(canonical(spec).encode()) != input_hash:
        raise ValueError('视频任务输入校验失败')
    context.progress('preparing')
    cached = context.checkpoint('video_result')
    if cached and all(valid(entry) for entry in cached.get('files', {}).values()) and cached.get('files'):
        result = cached
    else:
        folder = Path(storage_root).resolve() / 'video_runs' / task.task_id / ('attempt-' + uuid4().hex)
        folder.mkdir(parents=True, exist_ok=False)
        if file_hash(spec['font']['path']) != spec['font']['sha256']:
            raise ValueError('字体与任务提交时不同，请重新提交任务')
        # Freeze the actual bytes used for this attempt too.
        font = folder / ('font' + Path(spec['font']['path']).suffix)
        shutil.copyfile(spec['font']['path'], font)
        spec['font']['path'] = str(font)
        audio = make_audio(spec, folder, context)
        if Path(audio['audio']['path']) != folder / 'narration.wav':
            shutil.copyfile(audio['audio']['path'], folder / 'narration.wav')
        context.progress('subtitles', len(audio['cues']), len(audio['cues']))
        write_srt(folder / 'subtitles.srt', audio['cues'])
        width, height, fps = make_frames(spec, audio, folder, context)
        encode_video(folder, audio['duration'], fps, context)
        context.progress('verifying')
        info = probe(folder / 'video.mp4', check=context.check)
        video = next((s for s in info['streams'] if s['codec_type'] == 'video'), {})
        sound = next((s for s in info['streams'] if s['codec_type'] == 'audio'), {})
        actual_duration = float(info['format']['duration'])
        if (video.get('codec_name') != 'h264' or sound.get('codec_name') != 'aac'
                or video.get('width') != width or video.get('height') != height
                or abs(actual_duration - audio['duration']) > .3
                or abs(float(video.get('duration', 0)) - audio['duration']) > .15
                or abs(float(sound.get('duration', 0)) - audio['duration']) > .15
                or int(video.get('nb_frames', 0)) < int(audio['duration'] * fps) - 1):
            raise ValueError('成片规格或音视频时长校验失败')
        (folder / 'script_snapshot.json').write_text(canonical(spec['snapshot']), encoding='utf-8')
        names = ['video.mp4', 'narration.wav', 'subtitles.srt', 'cover.png', 'script_snapshot.json']
        if spec.get('scenes'):
            names.append('scene_timeline.json')
        manifest = {'schema_version': 1, 'kind': 'news2douyin.video', 'task_id': task.task_id,
                    'revision': revision, 'export_key': export_key, 'input_hash': input_hash,
                    'script_hash': spec['script_hash'], 'duration': actual_duration, 'width': width,
                    'height': height, 'fps': fps, 'alignment': audio['alignment'],
                    'backend': spec['options']['backend'], 'voice': spec['options']['voice'],
                    'template': spec.get('template', {'id':'legacy', 'version':1}), 'scene_count': len(spec.get('scenes', [])),
                    'image_assets': [{'asset_id': a['asset_id'], 'sha256': a['sha256']} for a in spec['images']],
                    'font_sha256': spec['font']['sha256'], 'files': {name: file_hash(folder / name) for name in names}}
        (folder / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        names.append('manifest.json')
        context.progress('packaging')
        with zipfile.ZipFile(folder / 'video_bundle.zip', 'w', zipfile.ZIP_STORED) as archive:
            for name in names:
                context.check()
                archive.write(folder / name, arcname=name)
        names.append('video_bundle.zip')
        result = {k: manifest[k] for k in ('duration', 'width', 'height', 'fps', 'alignment', 'revision', 'backend')}
        result['files'] = {name: artifact(folder / name) for name in names}
        context.save_checkpoint('video_result', result, replace=True)
    with Session(engine) as session:
        context.fence(session)
        row = session.get(VideoProduction, task.task_id)
        row.result_json = canonical(result)
        session.add(row)
        context.complete(session)
        session.commit()
