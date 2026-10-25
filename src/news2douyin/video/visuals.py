"""Pillow layouts used by both the setup preview and the final video frames."""
from __future__ import annotations


def draw_template(path, spec, size, cue, image_path=None):
    from PIL import Image, ImageDraw, ImageFont, ImageOps
    from .render import wrap_text
    width, height = size
    theme = spec['template']
    canvas = Image.new('RGB', size, theme['background'])
    draw = ImageDraw.Draw(canvas)
    face = spec['font']['path']
    def font(scale): return ImageFont.truetype(face, max(10, round(width * scale)))
    margin = round(width * .07)
    available = width - margin * 2
    def block(text, y, face, color, max_lines, x=margin, max_width=available):
        lines = wrap_text(text, face, max_width)
        if len(lines) > max_lines:
            lines = lines[:max_lines]; lines[-1] = lines[-1][:-1] + '…'
        for line in lines:
            draw.text((x, y), line, font=face, fill=color)
            y += face.size * 1.4
        return y
    scene = next((s for s in spec['scenes'] if s['scene_key'] == cue.get('scene_key')), spec['scenes'][0])
    title = spec['snapshot']['document']['title']
    test_face = font(.062)
    missing = bytes(test_face.getmask('\uffff'))
    visible = title + cue['text'] + scene['title'] + ''.join(d['label'] for d in scene['dates'])
    for char in set(visible):
        if '\u4e00' <= char <= '\u9fff' and bytes(test_face.getmask(char)) == missing:
            raise ValueError('所配置字体缺少中文字符，请设置 NEWS2DOUYIN_VIDEO_FONT')
    draw.rectangle((margin, height*.05, margin+width*.12, height*.057), fill=theme['accent'])
    draw.text((margin, height*.075), theme['name'] + '  /  NEWS2DOUYIN', font=font(.029), fill=theme['muted'])
    block(title, height*.115, test_face, theme['ink'], 3)
    badge = ('分析 / 推测' if scene['kind']=='analysis' else '事实陈述')
    draw.text((margin, height*.30), f"{scene['index']:02} / {len(spec['scenes']):02}   {badge}", font=font(.031), fill=theme['accent'])
    box = (margin, round(height*.355), width-margin, round(height*.685))
    draw.rounded_rectangle(box, radius=round(width*.023), fill=theme['panel'])
    if image_path:
        with Image.open(image_path) as img:
            canvas.paste(ImageOps.fit(img.convert('RGB'), (box[2]-box[0], box[3]-box[1])), box[:2])
        # Keep a readable label on top of imagery; it never claims the picture is evidence.
        label = '本段配图' if theme['layout']!='timeline' else (scene['dates'][0]['label'] if scene['dates'] else '本段未提供事件日期')
        draw.rectangle((box[0], box[3]-round(height*.043), box[2], box[3]), fill=theme['panel'])
        block(label, box[3]-round(height*.037), font(.028), theme['ink'], 1, x=box[0]+12, max_width=available-24)
    elif theme['layout']=='timeline':
        x=margin+round(width*.055)
        draw.line((x, height*.395, x, height*.635), fill=theme['accent'], width=max(2,round(width*.005)))
        draw.ellipse((x-5,height*.405-5,x+5,height*.405+5), fill=theme['accent'])
        block(scene['title'] or '本段进展',height*.38,font(.05),theme['ink'],2,x=x+18,max_width=available-45)
        labels='\n'.join(d['label'] for d in scene['dates']) or '本段未提供事件日期'
        block(labels,height*.515,font(.037),theme['muted'],3,x=x+18,max_width=available-45)
    else:
        label='如何理解这条进展' if theme['layout']=='explain' else '本段进展'
        draw.text((margin+18,height*.382),label,font=font(.03),fill=theme['accent'])
        block(scene['title'] or ('分析与限制' if scene['kind']=='analysis' else '新闻要点'),height*.437,font(.061),theme['ink'],3,x=margin+18,max_width=available-36)
        date=scene['dates'][0]['label'] if scene['dates'] else '请结合来源核对内容'
        block(date,height*.609,font(.029),theme['muted'],2,x=margin+18,max_width=available-36)
    for size_px in range(round(width*.055), round(width*.030)-1, -1):
        caption=ImageFont.truetype(face,max(size_px,10))
        lines=wrap_text(cue['text'],caption,available)
        orphan=len(lines)>1 and any(line and all(c in '。，、；：！？,.!?;:' for c in line) for line in lines)
        if len(lines)*caption.size*1.45 <= height*.18 and not orphan:break
    else:raise ValueError('单条字幕过长，请缩短 SRT 中的字幕分段')
    y=height*.724
    for line in lines:
        draw.text((margin,y),line,font=caption,fill=theme['ink']);y+=caption.size*1.45
    draw.line((margin,height*.922,width-margin,height*.922),fill=theme['accent'],width=1)
    labels='来源：'+' / '.join(scene['source_labels']) if scene['source_labels'] else '来源见审核稿'
    block(labels,height*.932,font(.026),theme['muted'],2)
    canvas.save(path)
