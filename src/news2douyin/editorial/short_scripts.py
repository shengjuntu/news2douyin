"""Small RunDesk material passes, ending in a source-bound editable short draft."""
from __future__ import annotations

import hashlib
import json
import shutil
import warnings
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlmodel import Session, select

from ..research import service as research
from ..research.models import ShortScriptBrief, ResearchSource, ResearchClaim, ResearchRun
from ..storage.models import ScriptPackage, ArticleEventLink
from ..video import workbench as wb
from .languages import language_instruction


class PreparationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    article_keys: list[str] = Field(min_length=1, max_length=6)
    output_language: Literal['zh-CN', 'en', 'ja'] = 'zh-CN'
    duration_sec: Literal[30, 60, 90, 120, 180] = 60
    find_images: bool = True
    angle: str = Field(default='', max_length=1500)
    idempotency_key: str = Field(min_length=1, max_length=128)

    @model_validator(mode='after')
    def distinct(self):
        if any(not k.strip() for k in self.article_keys) or len(set(self.article_keys)) != len(self.article_keys):
            raise ValueError('所选新闻不能为空或重复；最多合并 6 条相关报道')
        return self


class ShortSegment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['fact', 'analysis'] = 'fact'
    text: str = Field(min_length=1, max_length=3000)
    claim_ids: list[str] = Field(min_length=1, max_length=6)
    visual: str = Field(default='', max_length=2000)
    assets: str = Field(default='', max_length=2000)


class ImageChoice(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_id: str
    url: str = Field(min_length=1, max_length=2048)
    caption: str = Field(default='', max_length=500)


class ShortDraft(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=1, max_length=250)
    segments: list[ShortSegment] = Field(min_length=1, max_length=12)
    visual_notes: str = Field(default='', max_length=10000)
    notes: str = Field(default='', max_length=4000)
    gaps: list[str] = Field(default_factory=list, max_length=20)
    images: list[ImageChoice] = Field(default_factory=list, max_length=4)


def create(engine, payload):
    request = PreparationRequest.model_validate(payload)
    options = request.model_dump(exclude={'idempotency_key'})
    with Session(engine) as s:
        link = s.exec(select(ArticleEventLink).where(ArticleEventLink.article_key == request.article_keys[0])
                      .order_by(ArticleEventLink.id)).first()
        event_key = link.event_key if link else ''
    return research.create_case(engine, dict(article_keys=request.article_keys, event_key=event_key,
        goal='为短视频补充必要背景、合并相关来源并生成带引用的短稿。' + request.angle),
        preparation=dict(request_key=request.idempotency_key, request_hash=research.digest(options), options=options))


def prompt(options):
    restriction = ' 本次仅用已选资料写稿，禁止检索和新增 URL 读取；用已保存 source_id 读原文、保存判断与短稿。预算为0，下文一般补材建议不适用于本次。' if options.get('source_only') else ''
    return restriction + (' 本轮是短稿轻量补材任务，优先遵守这里的范围：不展开完整深度研究。'
            '先读所选原文，合并同一事件的事实；不相关的报道不要强行合并，写入 gaps。'
            '优先复用已保存资料和本地新闻。仅在补背景、核对冲突、寻找配图有必要时申请外部搜索；'
            '总计最多 3 次外部搜索、6 次新增正文读取，5 分钟补材预算，够用即停，不要求用完预算。'
            '搜索统一经过 research_search / Exa / research_record_search；原文用 research_read_source，事实用 research_save_claim 保存精确摘录。'
            '多个网站转载同一消息不算多方证实。口播保留归属、不确定性和冲突，不把搜索摘要当事实。'
            + language_instruction(options['output_language'], options['duration_sec']) +
            (' 需要配图：优先读取报道原页、机构公告或相关公开图片页面；可将必要的一次查询用于找配图页面。'
             'read_source 的 provenance.image_candidates 是真实页面发现的图片链接，请从中挑选最多 4 张相关图片；'
             '不能编造图片 URL，不能仅凭页面链接声称图片有授权。图片没有找到也必须交稿，并记录 gaps。'
             if options['find_images'] else ' 本次不寻找图片，images 必须为空。') +
            ' 最后调用 research_save_short_script 保存短稿，segments 每段关联已有 claim_ids；'
            '事实段不得引用 analysis/unverified 判断冒充已知事实。该工具会同时保存素材报告和脚本草稿，'
            '无需另外生成深度报告。达到时间预算仍可调用它交付已知事实和 gaps；没有可靠证据就保存 partial 报告说明原因。'
            '用户补充角度：' + options['angle'])


def list_preparations(engine):
    with Session(engine) as s:
        rows = s.exec(select(ShortScriptBrief).order_by(ShortScriptBrief.created_at.desc()).limit(30)).all()
        return [summary(s, r) for r in rows]


def summary(s, row):
    case = research.require_case(s, row.case_id)
    run = s.get(ResearchRun, case.active_run_id)
    return dict(case_id=row.case_id, title=case.title, options=json.loads(row.options_json),
                max_searches=json.loads(case.strategy_json)['max_searches'],max_reads=json.loads(case.strategy_json)['max_reads'],
                status=run.status if run else 'not_started', error=run.error if run else '',
                search_count=run.search_count if run else 0, read_count=run.read_count if run else 0,
                package_key=row.package_key, script_url='/scripts/' + row.package_key if row.package_key else None,
                created_at=row.created_at)


def _freeze(s, case, brief, draft):
    options = json.loads(brief.options_json)
    refs, moments, parts, choices = {}, {}, [], []
    groups = set()
    for index, segment in enumerate(draft.segments, 1):
        if len(set(segment.claim_ids)) != len(segment.claim_ids):
            raise ValueError('段落中的判断编号不能重复')
        ids = []
        for cid in segment.claim_ids:
            claim = s.get(ResearchClaim, cid)
            if not claim or claim.case_id != case.case_id:
                raise ValueError('短稿引用的判断不属于本次素材任务')
            evidence = json.loads(claim.evidence_json)
            if not evidence or not any(e.get('relation') == 'supports' for e in evidence):
                raise ValueError('口播引用的判断需要支持它的原文，只有背景或反证不能当作支持证据')
            if segment.kind == 'fact' and claim.kind not in {'fact', 'attributed'}:
                raise ValueError('事实段不能把推测或未证实判断当作事实')
            claim_ids = []
            for item in evidence:
                source = s.get(ResearchSource, item['source_id'])
                if not source or source.case_id != case.case_id:
                    raise ValueError('判断的原文快照缺失')
                if source.source_id not in refs:
                    provenance = json.loads(source.provenance_json)
                    group = provenance.get('independence_group') or source.content_hash
                    refs[source.source_id] = dict(evidence_id='E' + str(len(refs) + 1),
                        article_key=source.source_id, research_source_id=source.source_id,
                        article_revision=None, article_content_hash=source.content_hash,
                        title=source.title, url=source.url, published_at=source.published_at,
                        source_domain=urlsplit(source.url).hostname or '', is_duplicate=group in groups,
                        independence_group=group,
                        provenance=provenance, retrieved_at=source.retrieved_at,
                        paragraphs=json.loads(source.paragraphs_json), excerpts=[], excerpt='')
                    groups.add(group)
                ref = refs[source.source_id]
                if item['quote'] not in ref['excerpts']:
                    ref['excerpts'].append(item['quote']); ref['excerpt'] = '\n\n'.join(ref['excerpts'])
                claim_ids.append(ref['evidence_id'])
            claim_ids = list(dict.fromkeys(claim_ids))
            moments[cid] = dict(moment_key=cid, title=claim.text[:150], description=claim.text,
                kind=claim.kind, evidence=evidence, evidence_ids=claim_ids, reviewed=False,
                time_kind='unknown', date_start='', date_end='', certainty='unknown', time_note=claim.occurred_at)
            ids.extend(claim_ids)
        parts.append(dict(segment_key='p' + str(index), kind=segment.kind, text=segment.text,
            moment_keys=segment.claim_ids, evidence_ids=list(dict.fromkeys(ids)), visual=segment.visual, assets=segment.assets))
    if len(refs) > 20 or len(moments) > 30:
        raise ValueError('短稿最多保留 20 份来源、30 条证据判断')
    if draft.images and not options['find_images']:
        raise ValueError('本任务没有启用配图搜索')
    seen = set()
    for choice in draft.images:
        source = s.get(ResearchSource, choice.source_id)
        if not source or source.case_id != case.case_id:
            raise ValueError('配图页面不属于本次素材任务')
        found = next((i for i in json.loads(source.provenance_json).get('image_candidates', []) if i['url'] == choice.url), None)
        if not found:
            raise ValueError('图片 URL 必须来自已读取网页的 image_candidates，不接受模型自行编造链接')
        if choice.url not in seen:
            choices.append(dict(source_id=source.source_id, source_page_url=source.url,
                source_content_hash=source.content_hash, url=choice.url, caption=choice.caption,
                rights_status='unknown', usage='candidate_only'))
            seen.add(choice.url)
    document = wb.ScriptDocument(title=draft.title, output_language=options['output_language'],
        target_duration_sec=options['duration_sec'], script_text=wb.segment_narration(parts, options['output_language']),
        source_keys=list(refs), segments=parts, visual_notes=draft.visual_notes, notes=draft.notes).model_dump()
    evidence = dict(schema_version=1, origin='short_script_research', event_key=case.event_key, event_version=None,
        case_id=case.case_id, snapshot_at=research.utc_now_iso(), cutoff=case.cutoff,
        moments=list(moments.values()), sources=list(refs.values()))
    editorial = dict(event_key=case.event_key, event_title=draft.title, sources=list(refs.values()), evidence=evidence,
        generation=dict(mode='short_research', label='轻量补材 · RunDesk / Codex', **options),
        gaps=draft.gaps, image_candidates=[])
    wb._validate_sources(document, editorial['sources'], editorial)
    return document, editorial, choices


def fetch_images(choices, storage_root):
    if not choices:
        return []
    # At most four images. One slow host must not serialize all image reads.
    with ThreadPoolExecutor(max_workers=min(4, len(choices))) as pool:
        return list(pool.map(lambda choice: _fetch_image(choice, storage_root), choices))


def _fetch_image(choice, storage_root):
    from ..research.sources import safe_fetch
    folder = Path(storage_root) / 'short-script-images'
    item = dict(choice, status='unavailable')
    try:
        from PIL import Image
        final, mime, data = safe_fetch(choice['url'])
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as img:
                if img.format not in {'JPEG', 'PNG', 'WEBP'} or not (120 <= img.width <= 12000 and 90 <= img.height <= 12000) or img.width * img.height > 20_000_000:
                    raise ValueError('图片格式、尺寸不适合配图')
                fmt, width, height = img.format, img.width, img.height
                img.verify()
        digest = hashlib.sha256(data).hexdigest()
        filename = digest + {'JPEG': '.jpg', 'PNG': '.png', 'WEBP': '.webp'}[fmt]
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / filename
        if not path.exists():
            temporary = folder / (uuid4().hex + '.tmp')
            try:
                temporary.write_bytes(data); temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        item.update(status='downloaded', filename=filename, sha256=digest, width=width, height=height,
            archive_path='images/' + filename,
            final_url=final, mime_type={'JPEG': 'image/jpeg', 'PNG': 'image/png', 'WEBP': 'image/webp'}[fmt],
            retrieved_at=research.utc_now_iso())
    except ImportError:
        item['error'] = '图片组件未安装；请安装 news2douyin[research] 或 news2douyin[video]'
    except Exception:
        item['error'] = '配图读取或格式检查失败，已保留来源链接；可另选图片'
    return item

def image_bytes(storage_root, item):
    filename = item.get('filename', '')
    if item.get('status') != 'downloaded' or Path(filename).name != filename or not filename:
        raise KeyError('没有已下载的配图')
    data = (Path(storage_root) / 'short-script-images' / filename).read_bytes()
    if hashlib.sha256(data).hexdigest() != item['sha256']:
        raise ValueError('配图文件校验失败')
    return data


def save_draft(engine, settings, args):
    draft = ShortDraft.model_validate({k: v for k, v in args.items() if k != 'run_id'})
    fingerprint = research.digest(draft.model_dump())
    with Session(engine) as s:
        case, run = research.active_run(s, args['run_id'], enforce_time=False)
        brief = s.get(ShortScriptBrief, case.case_id)
        if not brief:
            raise ValueError('此工具仅供短稿轻量补材任务使用')
        if brief.package_key:
            return _saved(brief, fingerprint)
        _, _, choices = _freeze(s, case, brief, draft)
    storage_root = settings.path.parent.parent.resolve()
    images = fetch_images(choices, storage_root)
    folder, committed = None, False
    try:
        with Session(engine) as s:
            research.locked(s)
            case, run = research.active_run(s, args['run_id'], enforce_time=False)
            brief = s.get(ShortScriptBrief, case.case_id)
            if brief.package_key:
                return _saved(brief, fingerprint)
            document, editorial, _ = _freeze(s, case, brief, draft)
            editorial['image_candidates'] = images
            gaps = list(draft.gaps)
            if json.loads(brief.options_json)['find_images'] and not any(i['status'] == 'downloaded' for i in images):
                gaps.append('尚未取得可用配图；可先审核文字稿，后续另选素材。')
            elif any(i['status'] != 'downloaded' for i in images):
                gaps.append('部分配图获取失败，已保留来源页面，制作前需要另选素材。')
            editorial['gaps'] = gaps
            report = research.save_report(s, case, run, dict(title=draft.title,
                sections=[dict(heading=f'段落 {i}', body=p.text, claim_ids=p.claim_ids) for i, p in enumerate(draft.segments, 1)],
                gaps=gaps, completeness='partial' if gaps else 'complete'))
            editorial['research'] = dict(case_id=case.case_id, run_id=run.run_id, **report)
            key = 'pkg_' + case.case_id
            folder = Path(storage_root) / 'packages' / key / ('generation_' + uuid4().hex)
            package = ScriptPackage(package_key=key, event_key=case.event_key, profile_name='short_research',
                                    output_dir=str(folder), script_text=document['script_text'])
            wb.initialize_script(s, package, editorial, document=document)
            brief.package_key, brief.draft_hash, brief.report_id = key, fingerprint, report['report_id']
            s.add(brief)
            research.activity(s, case.case_id, 'short_script_saved', '短稿及逐段引用已保存，待人工审核', run.run_id)
            s.commit(); committed = True
            return _saved(brief, fingerprint)
    finally:
        if folder and not committed:
            shutil.rmtree(folder, ignore_errors=True)


def _saved(brief, fingerprint):
    if brief.draft_hash != fingerprint:
        raise ValueError('本任务已交付草稿；请到脚本工作台修改，不能用新结果覆盖已有稿件')
    return dict(package_key=brief.package_key, script_url='/scripts/' + brief.package_key,
                report_id=brief.report_id, review_status='draft')
